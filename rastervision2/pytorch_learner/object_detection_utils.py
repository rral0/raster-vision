from collections import defaultdict
from os.path import join
import tempfile

import torch
from torch.utils.data import Dataset
import torch.nn as nn
from torchvision.ops.boxes import batched_nms
from torchvision.models.detection.faster_rcnn import FasterRCNN
from torchvision.models.detection.backbone_utils import BackboneWithFPN
from torchvision.models import resnet
from torchvision.ops import misc as misc_nn_ops
import pycocotools
from pycocotools.coco import COCO
from pycocotools.cocoeval import COCOeval
import numpy as np
from PIL import Image

from rastervision2.pipeline.filesystem import file_to_json, json_to_file


def get_coco_gt(targets, num_labels):
    images = []
    annotations = []
    ann_id = 1
    for img_id, target in enumerate(targets, 1):
        # Use fake height, width, and filename because they don't matter.
        images.append({
            'id': img_id,
            'height': 1000,
            'width': 1000,
            'file_name': '{}.png'.format(img_id)
        })
        boxes, labels = target.boxes, target.get_field('labels')
        for box, label in zip(boxes, labels):
            box = box.float().tolist()
            label = label.item()
            annotations.append({
                'id':
                ann_id,
                'image_id':
                img_id,
                'category_id':
                label,
                'area': (box[2] - box[0]) * (box[3] - box[1]),
                'bbox': [box[1], box[0], box[3] - box[1], box[2] - box[0]],
                'iscrowd':
                0
            })
            ann_id += 1

    categories = [{
        'id': label,
        'name': str(label),
        'supercategory': 'super'
    } for label in range(num_labels)]
    coco = {
        'images': images,
        'annotations': annotations,
        'categories': categories
    }
    return coco


def get_coco_preds(outputs):
    preds = []
    for img_id, output in enumerate(outputs, 1):
        for box, label, score in zip(output.boxes, output.get_field('labels'),
                                     output.get_field('scores')):
            box = box.float().tolist()
            label = label.item()
            score = score.item()
            preds.append({
                'image_id':
                img_id,
                'category_id':
                label,
                'bbox': [box[1], box[0], box[3] - box[1], box[2] - box[0]],
                'score':
                score
            })
    return preds


def compute_coco_eval(outputs, targets, num_labels):
    """Return mAP averaged over 0.5-0.95 using pycocotools eval.

    Note: boxes are in (ymin, xmin, ymax, xmax) format with values ranging
        from 0 to h or w.

    Args:
        outputs: (list) of length m containing dicts of form
            {'boxes': <tensor with shape (n, 4)>,
             'labels': <tensor with shape (n,)>,
             'scores': <tensor with shape (n,)>}
        targets: (list) of length m containing dicts of form
            {'boxes': <tensor with shape (n, 4)>,
             'labels': <tensor with shape (n,)>}
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        preds = get_coco_preds(outputs)
        # ap is undefined when there are no predicted boxes
        if len(preds) == 0:
            return None

        gt = get_coco_gt(targets, num_labels)
        gt_path = join(tmp_dir, 'gt.json')
        json_to_file(gt, gt_path)
        coco_gt = COCO(gt_path)

        pycocotools.coco.unicode = None
        coco_preds = coco_gt.loadRes(preds)

        ann_type = 'bbox'
        coco_eval = COCOeval(coco_gt, coco_preds, ann_type)

        coco_eval.evaluate()
        coco_eval.accumulate()
        coco_eval.summarize()

        return coco_eval


def compute_class_f1(coco_eval):
    precision = coco_eval.eval['precision'][0, :, :, 0, -1]
    scores = coco_eval.eval['scores'][0, :, :, 0, -1]
    recall = np.linspace(0, 1, num=precision.shape[0])
    recall = recall[:, None]

    f1s = (2 * precision * recall) / (np.maximum(precision + recall, 1e-4))
    best_f1s = f1s.max(axis=0)
    best_f1_inds = f1s.argmax(axis=0)
    best_scores = scores[best_f1_inds, range(len(best_f1_inds))]

    return best_f1s, best_scores


def to_box_pixel(boxes, img_height, img_width):
    # convert from (ymin, xmin, ymax, xmax) in range [-1,1] to
    # range [0, h) or [0, w)
    boxes = ((boxes + 1.0) / 2.0) * torch.tensor(
        [[img_height, img_width, img_height, img_width]]).to(
            device=boxes.device, dtype=torch.float)
    return boxes


class BoxList():
    def __init__(self, boxes, **extras):
        """Constructor.

        Args:
            boxes: tensor<n, 4> with order ymin, xmin, ymax, xmax in pixels coords
            extras: dict with values that are tensors with first dimension corresponding
                to boxes first dimension
        """
        self.boxes = boxes
        self.extras = extras

    def get_field(self, name):
        if name == 'boxes':
            return self.boxes
        else:
            return self.extras.get(name)

    def _map_extras(self, func):
        new_extras = {}
        for k, v in self.extras.items():
            new_extras[k] = func(v)
        return new_extras

    def copy(self):
        return BoxList(self.boxes.copy(),
                       **self._map_extras(lambda x: x.copy()))

    def cpu(self):
        return BoxList(self.boxes.cpu(), **self._map_extras(lambda x: x.cpu()))

    def cuda(self):
        return BoxList(self.boxes.cuda(),
                       **self._map_extras(lambda x: x.cuda()))

    def to(self, device):
        return self.cpu() if device == 'cpu' else self.cuda()

    def xyxy(self):
        boxes = self.boxes[:, [1, 0, 3, 2]]
        return BoxList(boxes, **self.extras)

    def yxyx(self):
        boxes = self.boxes[:, [1, 0, 3, 2]]
        return BoxList(boxes, **self.extras)

    def __len__(self):
        return self.boxes.shape[0]

    @staticmethod
    def cat(box_lists):
        boxes = []
        extras = defaultdict(list)
        for bl in box_lists:
            boxes.append(bl.boxes)
            for k, v in bl.extras.items():
                extras[k].append(v)
        boxes = torch.cat(boxes)
        for k, v in extras.items():
            extras[k] = torch.cat(v)
        return BoxList(boxes, **extras)

    def equal(self, other):
        if len(other) != len(self):
            return False

        # Ignore order of boxes.
        extras = [(v.float().unsqueeze(1) if v.ndim == 1 else v.float())
                  for v in self.extras.values()]
        cat_arr = torch.cat([self.boxes] + extras, 1)
        self_tups = set([tuple([x.item() for x in row]) for row in cat_arr])

        extras = [(v.float().unsqueeze(1) if v.ndim == 1 else v.float())
                  for v in other.extras.values()]
        cat_arr = torch.cat([other.boxes] + extras, 1)
        other_tups = set([tuple([x.item() for x in row]) for row in cat_arr])
        return self_tups == other_tups

    def ind_filter(self, inds):
        new_extras = {}
        for k, v in self.extras.items():
            new_extras[k] = v[inds, ...]
        return BoxList(self.boxes[inds, :], **new_extras)

    def score_filter(self, score_thresh=0.25):
        scores = self.extras.get('scores')
        if scores is not None:
            return self.ind_filter(scores > score_thresh)
        else:
            raise ValueError('must have scores as key in extras')

    def clamp(self, img_height, img_width):
        boxes = torch.stack(
            [
                torch.clamp(self.boxes[:, 0], 0, img_height),
                torch.clamp(self.boxes[:, 1], 0, img_width),
                torch.clamp(self.boxes[:, 2], 0, img_height),
                torch.clamp(self.boxes[:, 3], 0, img_width)
            ],
            dim=1)
        return BoxList(boxes, **self.extras)

    def nms(self, iou_thresh=0.5):
        if len(self) == 0:
            return self

        good_inds = batched_nms(self.boxes, self.get_field('scores'),
                                self.get_field('labels'), iou_thresh)
        return self.ind_filter(good_inds)

    def scale(self, yscale, xscale):
        boxes = self.boxes * torch.tensor(
            [[yscale, xscale, yscale, xscale]], device=self.boxes.device)
        return BoxList(boxes, **self.extras)

    def pin_memory(self):
        self.boxes = self.boxes.pin_memory()
        for k, v in self.extras.items():
            self.extras[k] = v.pin_memory()
        return self

def collate_fn(data):
    x = [d[0].unsqueeze(0) for d in data]
    y = [d[1] for d in data]
    return (torch.cat(x), y)


class CocoDataset(Dataset):
    def __init__(self, img_dir, annotation_uris, transform=None):
        self.img_dir = img_dir
        self.annotation_uris = annotation_uris
        self.transform = transform

        self.imgs = []
        self.img2id = {}
        self.id2img = {}
        self.id2boxes = defaultdict(lambda: [])
        self.id2labels = defaultdict(lambda: [])
        self.label2name = {}
        for annotation_uri in annotation_uris:
            ann_json = file_to_json(annotation_uri)
            for img in ann_json['images']:
                self.imgs.append(img['file_name'])
                self.img2id[img['file_name']] = img['id']
                self.id2img[img['id']] = img['file_name']
            for ann in ann_json['annotations']:
                img_id = ann['image_id']
                box = ann['bbox']
                label = ann['category_id']
                box = torch.tensor(
                    [[box[1], box[0], box[1] + box[3], box[0] + box[2]]])
                self.id2boxes[img_id].append(box)
                self.id2labels[img_id].append(label)
        self.id2boxes = dict([(id, torch.cat(boxes).float())
                              for id, boxes in self.id2boxes.items()])
        self.id2labels = dict([(id, torch.tensor(labels))
                               for id, labels in self.id2labels.items()])

    def __getitem__(self, ind):
        img_fn = self.imgs[ind]
        img_id = self.img2id[img_fn]
        img = Image.open(join(self.img_dir, img_fn))

        if img_id in self.id2boxes:
            boxes, labels = self.id2boxes[img_id], self.id2labels[img_id]
            boxlist = BoxList(boxes, labels=labels)
        else:
            boxlist = BoxList(
                torch.empty((0, 4)), labels=torch.empty((0, )).long())
        if self.transform:
            return self.transform(img, boxlist)
        return (img, boxlist)

    def __len__(self):
        return len(self.imgs)


def get_out_channels(model):
    out = {}

    def make_save_output(layer_name):
        def save_output(layer, input, output):
            out[layer_name] = output.shape[1]

        return save_output

    model.layer1.register_forward_hook(make_save_output('layer1'))
    model.layer2.register_forward_hook(make_save_output('layer2'))
    model.layer3.register_forward_hook(make_save_output('layer3'))
    model.layer4.register_forward_hook(make_save_output('layer4'))

    model(torch.empty((1, 3, 128, 128)))
    return [out['layer1'], out['layer2'], out['layer3'], out['layer4']]


# This fixes a bug in torchvision.
def resnet_fpn_backbone(backbone_name, pretrained):
    backbone = resnet.__dict__[backbone_name](
        pretrained=pretrained, norm_layer=misc_nn_ops.FrozenBatchNorm2d)

    # freeze layers
    for name, parameter in backbone.named_parameters():
        if 'layer2' not in name and 'layer3' not in name and 'layer4' not in name:
            parameter.requires_grad_(False)

    return_layers = {'layer1': 0, 'layer2': 1, 'layer3': 2, 'layer4': 3}

    out_channels = 256
    in_channels_list = get_out_channels(backbone)
    return BackboneWithFPN(backbone, return_layers, in_channels_list,
                           out_channels)


class MyFasterRCNN(nn.Module):
    """Adapter around torchvision Faster-RCNN.

    The purpose of the adapter is to use a different input and output format
    and inject bogus boxes to circumvent torchvision's inability to handle
    training examples with no ground truth boxes.
    """

    def __init__(self, backbone_arch, num_labels, img_sz, pretrained=True):
        super().__init__()

        backbone = resnet_fpn_backbone(backbone_arch, pretrained)
        self.model = FasterRCNN(
            backbone, num_labels, min_size=img_sz, max_size=img_sz)
        self.subloss_names = [
            'total_loss', 'loss_box_reg', 'loss_classifier', 'loss_objectness',
            'loss_rpn_box_reg'
        ]

    def forward(self, input, targets=None):
        """Forward pass

        Args:
            input: tensor<n, 3, h, w> with batch of images
            targets: None or list<BoxList> of length n with boxes and labels

        Returns:
            if targets is None, returns list<BoxList> of length n, containing
            boxes, labels, and scores for boxes with score > 0.05. Further
            filtering based on score should be done before considering the
            prediction "final".

            if targets is a list, returns the losses as dict with keys from
            self.subloss_names.
        """
        if targets:
            # Add bogus background class box for each image to workaround
            # the inability of torchvision to train on images with
            # no ground truth boxes. This is important for being able
            # to handle negative chips generated by RV.
            new_targets = []
            for x, y in zip(input, targets):
                h, w = x.shape[1:]
                boxes = torch.cat(
                    [
                        y.boxes,
                        torch.tensor([[0., 0, h, w]], device=input.device)
                    ],
                    dim=0)
                labels = torch.cat(
                    [
                        y.get_field('labels'),
                        torch.tensor([0], device=input.device)
                    ],
                    dim=0)
                bl = BoxList(boxes, labels=labels)
                new_targets.append(bl)
            targets = new_targets

            _targets = [bl.xyxy() for bl in targets]
            _targets = [{
                'boxes': bl.boxes,
                'labels': bl.get_field('labels')
            } for bl in _targets]
            loss_dict = self.model(input, _targets)
            loss_dict['total_loss'] = sum(list(loss_dict.values()))
            return loss_dict

        out = self.model(input)
        boxlists = [
            BoxList(
                _out['boxes'], labels=_out['labels'],
                scores=_out['scores']).yxyx() for _out in out
        ]

        # Remove bogus background boxes.
        new_boxlists = []
        for bl in boxlists:
            labels = bl.get_field('labels')
            non_zero_inds = labels != 0
            new_boxlists.append(bl.ind_filter(non_zero_inds))
        return new_boxlists