# Yeah, try scheduling another training loop. We need to:

# 1. Fix the training script
# 2. Update the SegModule to only use the 9 feature maps -- OK
# 3. Check if it trains
# 4. Figure out how long it takes to train
# 5. Maybe run a couple epochs and test if it does anything

# ------------

# Other TODOS:

# 1. Unpack and analyze CGD results (just cmnist is fine I guess)
# 2. Schedule hyperparameters evaluation for the CGD step
# 3. ???
# 4. Profit

import os
import torch
import random
import torchvision
import numpy as np
import torch.nn as nn
import torch.optim as optim

from datetime import datetime
from pytorch_lightning import seed_everything
from diffusers import StableDiffusionPipeline
from torch.utils.tensorboard import SummaryWriter
from grounded_unet import GroundedUNet2DConditionModel

from seg_module import Segmodule
from utils import TrainingType, preprocess_mask
from mmdet.apis import init_detector, inference_detector


seed = 42
temp_dir = "temp"
outputs_dir = "outputs"
checkpoints_dir = "checkpoints"
device = torch.device("cuda")
pascal_class_split = 1
model_name = "runwayml/stable-diffusion-v1-5"
model_type = model_name.split("/")[-1]

# FIXME: Not fully implemented
batch_size = 1
learning_rate = 1e-5
total_epochs = 500000
training_data_type = TrainingType.SINGLE

os.environ["CUDA_VISIBLE_DEVICES"] = "3"

mask_rnn_config = {
  "config": "mmdetection/configs/swin/mask_rcnn_swin-s-p4-w7_fpn_fp16_ms-crop-3x_coco.py",
  "checkpoint": "mmdetection/checkpoint/mask_rcnn_swin-s-p4-w7_fpn_fp16_ms-crop-3x_coco_20210903_104808-b92c91f1.pth"
}

seed_everything(seed)

os.makedirs(temp_dir, exist_ok=True)
os.makedirs(outputs_dir, exist_ok=True)
os.makedirs(checkpoints_dir, exist_ok=True)

# Load COCO and Pascal-VOC classes
coco_classes = open("mmdetection/demo/coco_80_class.txt").read().split("\n")
coco_classes = dict([(c, i) for i, c in enumerate(coco_classes)])

pascal_classes = open(f"VOC/class_split{pascal_class_split}.csv").read().split("\n")
pascal_classes = [c.split(",")[0] for c in pascal_classes]

train_classes, test_classes = pascal_classes[:15], pascal_classes[15:]

# Load Mask R-CNN
pretrain_detector = init_detector(
  mask_rnn_config["config"],
  mask_rnn_config["checkpoint"],
  device=device
)

# Load the segmentation module
seg_module = Segmodule().to(device)

# Load the stable diffusion pipeline
pipeline = StableDiffusionPipeline.from_pretrained(model_name).to(device)

# Save the pretrained UNet to disk
unet_model_dir = os.path.join("unet_model", model_type)
pretrained_unet_dir = os.path.join(temp_dir, unet_model_dir)

pipeline_components = pipeline.components

if not os.path.isdir(pretrained_unet_dir):
  pipeline_components["unet"].save_pretrained(pretrained_unet_dir)

# Reload the UNet as the grounded subclass
grounded_unet = GroundedUNet2DConditionModel.from_pretrained(
    pretrained_unet_dir
).to(device)

pipeline_components["unet"] = grounded_unet

pipeline = StableDiffusionPipeline(**pipeline_components)

# Setup tokenizer and the CLIP embedder
tokenizer = pipeline_components["tokenizer"]
embedder = pipeline_components["text_encoder"]

def get_embeddings(prompt: str):
  tokens = tokenizer(prompt, return_tensors="pt")

  tokens["input_ids"] = tokens["input_ids"].to("cuda")
  tokens["attention_mask"] = tokens["attention_mask"].to("cuda")

  token_embeddings = embedder(**tokens).last_hidden_state
  token_embeddings = token_embeddings[:, len(tokens["input_ids"]), :].to(device)

  return token_embeddings.repeat(batch_size, 1, 1)

# Start training
print(f"starting training for up to {total_epochs} epochs")

current_time = datetime.now().strftime("%b%d_%H-%M-%S")

# Create folders to store checkpoints, training data, etc.
run_dir = os.path.join(checkpoints_dir, f"run-{current_time}")

run_logs_dir = os.path.join(run_dir, "logs")
training_dir = os.path.join(run_dir, "training")

os.makedirs(run_dir, exist_ok=True)
os.makedirs(training_dir, exist_ok=True)

# Setup logger, optimizer and loss
torch_writer = SummaryWriter(log_dir=run_logs_dir)

loss_fn = nn.BCEWithLogitsLoss()
optimizer = optim.Adam(params=seg_module.parameters(), lr=learning_rate)

# FIXME: Here we only implement the SINGLE train strategy.
# We should also implement the others
if training_data_type != TrainingType.SINGLE:
  raise ValueError(f"invalid training type {training_data_type}")

for epoch in range(total_epochs):
  print(f"starting epoch {epoch}")

  picked_class = random.choice(train_classes)
  prompt = f"a photograph of a {picked_class}"

  grounded_unet.clear_grounding_features()

  # Sample the image
  image = pipeline(prompt).images[0]
  array_image = np.array(image)

  # Get the UNet features
  unet_features = grounded_unet.get_grounding_features()
  prompt_embeddings = get_embeddings(prompt=prompt)

  # Segment the image with Mask R-CNN
  # segmentation should be a list of masks, one per class
  _, segmentation = inference_detector(
    pretrain_detector,
    [array_image]
  ).pop()

  # segmented_classes = [
  #   (i, x) for i, x in enumerate(segmentation) if len(x) > 0
  # ]

  # FIXME: We only have a single trainclass for
  # now, eventually this will become a loop
  fusion_segmentation = seg_module(unet_features, prompt_embeddings)

  class_index = coco_classes[picked_class]

  fusion_segmentation_pred = torch.unsqueeze(
    fusion_segmentation[0, 0, :, :],
    0
  ).unsqueeze(0)

  fusion_mask = preprocess_mask(mask=fusion_segmentation_pred)

  # Save the fusion module prediction every 200 epochs
  if epoch % 200 == 0:
    image.save(os.path.join(training_dir, f"sd_image_{epoch}_{picked_class}.png"))

    torchvision.utils.save_image(
      torch.from_numpy(fusion_mask),
      os.path.join(training_dir, f"vis_sample_{epoch}_{picked_class}_pred_seg.png"),
      normalize=True,
      scale_each=True
    )

  if len(segmentation[class_index]) == 0:
    print(f"the pretrained detector failed to detect objects for class {class_name}")
  else:
    segmented_class = torch.from_numpy(segmentation[class_index][0].astype(int))
    segmented_class = segmented_class.float().unsqueeze(0).unsqueeze(0).cuda()

    loss = loss_fn(fusion_segmentation_pred, segmented_class)

    optimizer.zero_grad()
    loss.backward()
    optimizer.step()

    torch_writer.add_scalar("train/loss", loss.item(), global_step=epoch)

    print(f"training step: {epoch}/{total_epochs}, loss: {loss}")

    segmentation_gt = segmented_class[0][0].cpu()

    if epoch % 200 == 0:
      mask_visualization = torch.cat([segmentation_gt, torch.from_numpy(fusion_mask).squeeze()], axis=1)

      torchvision.utils.save_image(
        mask_visualization,
        os.path.join(training_dir, f"vis_sample_segmentation_{epoch}_{picked_class}.png"),
        normalize=True,
        scale_each=True
      )

  # FIXME: Increase the steps between saves
  if epoch % 50 == 0:
    print(f"saving checkpoint...")

    torch.save(seg_module.state_dict(), os.path.join(run_dir, f"checkpoint_{epoch}.pth"))
