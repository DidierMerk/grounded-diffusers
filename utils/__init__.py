import os
import torch
import numpy as np

from enum import Enum
from typing import Tuple, List

from diffusers import StableDiffusionPipeline
from grounded_unet import GroundedUNet2DConditionModel


class TrainingType(Enum):
  SINGLE = "single"
  TWO = "two"
  RANDOM = "random"


def plot_mask(img, masks, colors=None, alpha=0.8,indexlist=[0,1]) -> np.ndarray:
    """Visualize segmentation mask.

    Parameters
    ----------
    img: numpy.ndarray
        Image with shape `(H, W, 3)`.
    masks: numpy.ndarray
        Binary images with shape `(N, H, W)`.
    colors: numpy.ndarray
        corlor for mask, shape `(N, 3)`.
        if None, generate random color for mask
    alpha: float, optional, default 0.5
        Transparency of plotted mask

    Returns
    -------
    numpy.ndarray
        The image plotted with segmentation masks, shape `(H, W, 3)`

    """
    H, W = masks.shape[0], masks.shape[1]
    color_list=[[255,97,0],[128,42,42],[220,220,220],[255,153,18],[56,94,15],[127,255,212],[210,180,140],[221,160,221],[255,0,0],[255,128,0],[255,255,0],[128,255,0],[0,255,0],[0,255,128],[0,255,255],[0,128,255],[0,0,255],[128,0,255],[255,0,255],[255,0,128]]*6
    final_color_list=[np.array([[i]*512]*512) for i in color_list]
    
    background=np.ones(img.shape)*255
    count=0
    colors=final_color_list[indexlist[count]]

    for mask, color in zip(masks, colors):
        color=final_color_list[indexlist[count]]
        mask = np.stack([mask, mask, mask], -1)
        img = np.where(mask, img * (1 - alpha) + color * alpha,background*0.4+img*0.6 )
        count+=1

    return img.astype(np.uint8)


def calculate_iou(mask1, mask2):
    assert mask1.shape == mask2.shape, "Masks must have the same shape."

    # Calculate intersection and union.
    intersection = np.logical_and(mask1, mask2)
    union = np.logical_or(mask1, mask2)

    # Count the number of pixels in the intersection and union.
    intersection_count = np.sum(intersection)
    union_count = np.sum(union)

    # Avoid division by zero.
    if union_count == 0:
        return 0

    iou = intersection_count / union_count

    return iou


def preprocess_mask(mask: torch.Tensor):
    probabilities = torch.sigmoid(mask)

    mask = torch.zeros_like(probabilities, dtype=torch.float32)
    mask[probabilities > 0.5] = 1

    return mask[0].cpu().numpy()


def load_stable_diffusion(
    model_name: str,
    device: torch.device,
    temp_dir="temp"
) -> Tuple[StableDiffusionPipeline, GroundedUNet2DConditionModel]:
    # Load the stable diffusion pipeline
    pipeline = StableDiffusionPipeline.from_pretrained(model_name).to(device)

    # Save the pretrained UNet to disk
    model_type = model_name.split("/")[-1]

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

    return StableDiffusionPipeline(**pipeline_components), grounded_unet


def get_embeddings(tokenizer, embedder, device: torch.device, prompt: str, batch_size: int):
  tokens = tokenizer(prompt, return_tensors="pt")

  tokens["input_ids"] = tokens["input_ids"].to(device)
  tokens["attention_mask"] = tokens["attention_mask"].to(device)

  token_embeddings = embedder(**tokens).last_hidden_state
  token_embeddings = token_embeddings[:, len(tokens["input_ids"]), :].to(device)

  return token_embeddings.repeat(batch_size, 1, 1)


def has_mask_for_classes(masks: List[List], class_indices: List[int]) -> bool:
    """
        Returns whether the Mask R-CNN segmentation has
        a mask for all the given classes. masks is an array
        of masks (one per available class) as returned by
        Mask R-CNN.
    """
    for class_index in class_indices:
      # Get Mask R-CNN mask tensor
      if len(masks[class_index]) == 0:
        return False

    return True
