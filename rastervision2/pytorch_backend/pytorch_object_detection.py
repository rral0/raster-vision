from os.path import join
import uuid

from rastervision2.pipeline.filesystem import (make_dir, json_to_file)
from rastervision2.core.data.label import ObjectDetectionLabels
from rastervision2.core.utils.misc import save_img
from rastervision2.core.data_sample import DataSample
from rastervision2.pytorch_backend.pytorch_learner_backend import (
    PyTorchLearnerSampleWriter, PyTorchLearnerBackend)


class PyTorchObjectDetectionSampleWriter(PyTorchLearnerSampleWriter):
    def __enter__(self):
        super().__enter__()

        self.splits = {
            'train': {
                'images': [],
                'annotations': []
            },
            'valid': {
                'images': [],
                'annotations': []
            }
        }
        self.categories = [{
            'id': class_id,
            'name': class_name
        } for class_id, class_name in enumerate(self.class_config.names)]

        return self

    def __exit__(self, type, value, traceback):
        for split in ['train', 'valid']:
            if len(self.splits[split]['images']) > 0:
                split_dir = join(self.sample_dir, split)
                labels_path = join(split_dir, 'labels.json')

                images = self.splits[split]['images']
                annotations = self.splits[split]['annotations']
                coco_dict = {
                    'images': images,
                    'annotations': annotations,
                    'categories': self.categories
                }
                json_to_file(coco_dict, labels_path)

        super().__exit__(type, value, traceback)

    def write_sample(self, sample: DataSample):
        """
        This writes a training or validation sample to
        (train|valid)/img/{scene_id}-{ind}.png and updates
        some COCO data structures.
        """
        split = 'train' if sample.is_train else 'valid'
        split_dir = join(self.sample_dir, split)
        img_dir = join(split_dir, 'img')
        make_dir(img_dir)
        img_fn = '{}-{}.png'.format(sample.scene_id, self.sample_ind)
        img_path = join(img_dir, img_fn)
        save_img(sample.chip, img_path)

        images = self.splits[split]['images']
        annotations = self.splits[split]['annotations']

        images.append({
            'file_name': img_fn,
            'id': self.sample_ind,
            'height': sample.chip.shape[0],
            'width': sample.chip.shape[1]
        })

        npboxes = sample.labels.get_npboxes()
        npboxes = ObjectDetectionLabels.global_to_local(npboxes, sample.window)
        for box_ind, (box, class_id) in enumerate(
                zip(npboxes, sample.labels.get_class_ids())):
            bbox = [box[1], box[0], box[3] - box[1], box[2] - box[0]]
            bbox = [int(i) for i in bbox]
            annotations.append({
                'id': '{}-{}'.format(self.sample_ind, box_ind),
                'image_id': self.sample_ind,
                'bbox': bbox,
                'category_id': int(class_id)
            })

        self.sample_ind += 1


class PyTorchObjectDetection(PyTorchLearnerBackend):
    def get_sample_writer(self):
        output_uri = join(self.pipeline_cfg.chip_uri, '{}.zip'.format(
            str(uuid.uuid4())))
        return PyTorchObjectDetectionSampleWriter(
            output_uri, self.pipeline_cfg.dataset.class_config, self.tmp_dir)

    def predict(self, chips, windows):
        if self.learner is None:
            self.load_model()

        batch_out = self.learner.numpy_predict(chips, raw_out=False)
        labels = ObjectDetectionLabels()
        for out, window in zip(batch_out, windows):
            labels.set_label_arr(window, out)

        return labels
