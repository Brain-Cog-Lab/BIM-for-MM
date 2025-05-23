import os, warnings
import time

import tonic
from tonic import DiskCachedDataset, RandomChoiceDiskCachedDataset

import torch
import torch.nn.functional as F
import torch.utils
import torchvision.datasets as datasets
from timm.data import ImageDataset, create_loader, Mixup, FastCollateMixup, AugMixDataset
from timm.data import create_transform

import PIL
import torchaudio
import librosa
import numpy as np
import torchvision
import csv
import copy

from torchvision import transforms
from typing import Any, Dict, Optional, Sequence, Tuple, Union

import braincog
from braincog.datasets.NOmniglot.nomniglot_full import NOmniglotfull
from braincog.datasets.NOmniglot.nomniglot_nw_ks import NOmniglotNWayKShot
from braincog.datasets.NOmniglot.nomniglot_pair import NOmniglotTrainSet, NOmniglotTestSet
from braincog.datasets.ESimagenet.ES_imagenet import ESImagenet_Dataset
from braincog.datasets.ESimagenet.reconstructed_ES_imagenet import ESImagenet2D_Dataset
from braincog.datasets.CUB2002011 import CUB2002011
from braincog.datasets.TinyImageNet import TinyImageNet
from braincog.datasets.StanfordDogs import StanfordDogs
from braincog.datasets.bullying10k import BULLYINGDVS
from braincog.datasets.time_conut import TimeCounter

from braincog.datasets.cut_mix import CutMix, EventMix, MixUp
from braincog.datasets.rand_aug import *
from braincog.datasets.utils import dvs_channel_check_expend, rescale

from torch.utils.data import ConcatDataset, Subset
from collections import defaultdict

DVSCIFAR10_MEAN_16 = [0.3290, 0.4507]
DVSCIFAR10_STD_16 = [1.8398, 1.6549]

# DATA_DIR = '/mnt/home/hexiang/datasets/'
DATA_DIR = '/mnt/data/datasets/'
# DATA_DIR = '/home/hexiang/data/'

DEFAULT_CROP_PCT = 0.875
IMAGENET_DEFAULT_MEAN = (0.485, 0.456, 0.406)
IMAGENET_DEFAULT_STD = (0.229, 0.224, 0.225)
IMAGENET_INCEPTION_MEAN = (0.5, 0.5, 0.5)
IMAGENET_INCEPTION_STD = (0.5, 0.5, 0.5)
IMAGENET_DPN_MEAN = (124 / 255, 117 / 255, 104 / 255)
IMAGENET_DPN_STD = tuple([1 / (.0167 * 255)] * 3)

CIFAR10_DEFAULT_MEAN = (0.4914, 0.4822, 0.4465)
CIFAR10_DEFAULT_STD = (0.2023, 0.1994, 0.2010)


class MySampler(torch.utils.data.sampler.Sampler):
    r"""Samples elements randomly from a given list of indices, without replacement.
    Arguments:
        indices (sequence): a sequence of indices
    """

    def __init__(self, indices):
        self.indices = indices

    def __iter__(self):
        return (self.indices[i] for i in range(len(self.indices)))

    def __len__(self):
        return len(self.indices)


class MyDataSet(torchvision.datasets.VisionDataset):
    def __init__(self, data, label):
        self.data = data
        self.label = label
        self.length = data.shape[0]

    def __getitem__(self, mask):
        data = self.data[mask]
        label = self.label[mask]
        return data, label

    def __len__(self):
        return self.length


class MyAVDataSet(torchvision.datasets.VisionDataset):
    def __init__(self, data, label):
        self.data = data
        self.label = label
        self.length = len(data)

    def __getitem__(self, mask):
        audio, viusal = self.data[mask]
        label = self.label[mask]
        return (audio, viusal), label

    def __len__(self):
        return self.length


def unpack_mix_param(args):
    mix_up = args['mix_up'] if 'mix_up' in args else False
    cut_mix = args['cut_mix'] if 'cut_mix' in args else False
    event_mix = args['event_mix'] if 'event_mix' in args else False
    beta = args['beta'] if 'beta' in args else 1.
    prob = args['prob'] if 'prob' in args else .5
    num = args['num'] if 'num' in args else 1
    num_classes = args['num_classes'] if 'num_classes' in args else 10
    noise = args['noise'] if 'noise' in args else 0.
    gaussian_n = args['gaussian_n'] if 'gaussian_n' in args else None
    return mix_up, cut_mix, event_mix, beta, prob, num, num_classes, noise, gaussian_n


def build_transform(is_train, img_size):
    """
    构建数据增强, 适用于static data
    :param is_train: 是否训练集
    :param img_size: 输出的图像尺寸
    :return: 数据增强策略
    """
    resize_im = img_size > 32
    if is_train:
        # this should always dispatch to transforms_imagenet_train
        transform = create_transform(
            input_size=img_size,
            is_training=True,
            color_jitter=0.4,
            auto_augment='rand-m9-mstd0.5-inc1',
            interpolation='bicubic',
            re_prob=0.25,
            re_mode='pixel',
            re_count=1,
        )
        if not resize_im:
            # replace RandomResizedCropAndInterpolation with
            # RandomCrop
            transform.transforms[0] = transforms.RandomCrop(
                img_size, padding=4)
        return transform

    t = []
    if resize_im:
        size = int((256 / 224) * img_size)
        t.append(
            # to maintain same ratio w.r.t. 224 images
            transforms.Resize(size, interpolation=3),
        )
        t.append(transforms.CenterCrop(img_size))

    t.append(transforms.ToTensor())
    if img_size > 32:
        t.append(transforms.Normalize(IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD))
    else:
        t.append(transforms.Normalize(CIFAR10_DEFAULT_MEAN, CIFAR10_DEFAULT_STD))
    return transforms.Compose(t)


def build_dataset(is_train, img_size, dataset, path, same_da=False):
    """
    构建带有增强策略的数据集
    :param is_train: 是否训练集
    :param img_size: 输出图像尺寸
    :param dataset: 数据集名称
    :param path: 数据集路径
    :param same_da: 为训练集使用测试集的增广方法
    :return: 增强后的数据集
    """
    transform = build_transform(False, img_size) if same_da else build_transform(is_train, img_size)

    if dataset == 'CIFAR10':
        dataset = datasets.CIFAR10(
            path, train=is_train, transform=transform, download=True)
        nb_classes = 10
    elif dataset == 'CIFAR100':
        dataset = datasets.CIFAR100(
            path, train=is_train, transform=transform, download=True)
        nb_classes = 100
    else:
        raise NotImplementedError

    return dataset, nb_classes


class MNISTData(object):
    """
    Load MNIST datesets.
    """

    def __init__(self,
                 data_path: str,
                 batch_size: int,
                 train_trans: Sequence[torch.nn.Module] = None,
                 test_trans: Sequence[torch.nn.Module] = None,
                 pin_memory: bool = True,
                 drop_last: bool = True,
                 shuffle: bool = True,
                 ) -> None:
        self._data_path = data_path
        self._batch_size = batch_size
        self._pin_memory = pin_memory
        self._drop_last = drop_last
        self._shuffle = shuffle
        self._train_transform = transforms.Compose(train_trans) if train_trans else None
        self._test_transform = transforms.Compose(test_trans) if test_trans else None

    def get_data_loaders(self):
        print('Batch size: ', self._batch_size)
        train_datasets = datasets.MNIST(root=self._data_path, train=True, transform=self._train_transform,
                                        download=True)
        test_datasets = datasets.MNIST(root=self._data_path, train=False, transform=self._test_transform, download=True)
        train_loader = torch.utils.data.DataLoader(
            train_datasets, batch_size=self._batch_size,
            pin_memory=self._pin_memory, drop_last=self._drop_last, shuffle=self._shuffle
        )
        test_loader = torch.utils.data.DataLoader(
            test_datasets, batch_size=self._batch_size,
            pin_memory=self._pin_memory, drop_last=False
        )
        return train_loader, test_loader

    def get_standard_data(self):
        MNIST_MEAN = 0.1307
        MNIST_STD = 0.3081
        self._train_transform = transforms.Compose([transforms.RandomCrop(28, padding=4),
                                                    transforms.ToTensor(),
                                                    transforms.Normalize((MNIST_MEAN,), (MNIST_STD,))])
        self._test_transform = transforms.Compose([transforms.ToTensor(),
                                                   transforms.Normalize((MNIST_MEAN,), (MNIST_STD,))])
        return self.get_data_loaders()


def get_mnist_data(batch_size, num_workers=8, same_da=False, root=DATA_DIR, **kwargs):
    """
    获取MNIST数据
    http://data.pymvpa.org/datasets/mnist/
    :param batch_size: batch size
    :param same_da: 为训练集使用测试集的增广方法
    :param kwargs:
    :return: (train loader, test loader, mixup_active, mixup_fn)
    """
    MNIST_MEAN = 0.1307
    MNIST_STD = 0.3081
    if 'root' in kwargs: root = kwargs["root"]
    if 'skip_norm' in kwargs and kwargs['skip_norm'] is True:
        train_transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Lambda(rescale)
        ])
        test_transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Lambda(rescale)
        ])
    else:
        train_transform = transforms.Compose([transforms.RandomCrop(28, padding=4),
                                              # transforms.RandomRotation(10),
                                              transforms.ToTensor(),
                                              transforms.Normalize((MNIST_MEAN,), (MNIST_STD,))])
        test_transform = transforms.Compose([transforms.ToTensor(),
                                             transforms.Normalize((MNIST_MEAN,), (MNIST_STD,))])

    train_datasets = datasets.MNIST(
        root=root, train=True, transform=test_transform if same_da else train_transform, download=True)
    test_datasets = datasets.MNIST(
        root=root, train=False, transform=test_transform, download=True)

    train_loader = torch.utils.data.DataLoader(
        train_datasets, batch_size=batch_size,
        pin_memory=True, drop_last=True, shuffle=True, num_workers=num_workers
    )

    test_loader = torch.utils.data.DataLoader(
        test_datasets, batch_size=batch_size,
        pin_memory=True, drop_last=False, num_workers=num_workers
    )

    return train_loader, test_loader, False, None


def get_mnistdvs_data(batch_size, step, dvs_da=False, **kwargs):
    """
    获取MNIST-DVS数据
    http://journal.frontiersin.org/Article/10.3389/fnins.2015.00437/abstract
    :param batch_size: batch size
    :param step: 仿真步长
    :param kwargs:
    :return: (train loader, test loader, mixup_active, mixup_fn)
    """
    sensor_size = tonic.datasets.MNISTDVS.sensor_size
    size = kwargs['size'] if 'size' in kwargs else 26

    train_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        # tonic.transforms.DropEvent(p=0.1),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step),
    ])
    test_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step),
    ])

    train_dataset = tonic.datasets.MNISTDVS(os.path.join(DATA_DIR, 'DVS/MNIST_DVS/'),
                                            transform=train_transform, train=True)

    test_dataset = tonic.datasets.MNISTDVS(os.path.join(DATA_DIR, 'DVS/MNIST_DVS/'),
                                           transform=test_transform, train=False)

    train_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        lambda x: F.interpolate(x, size=[size, size], mode='bilinear', align_corners=True),
    ])
    test_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        lambda x: F.interpolate(x, size=[size, size], mode='bilinear', align_corners=True),
    ])

    train_dataset = DiskCachedDataset(train_dataset,
                                      cache_path=os.path.join(DATA_DIR, 'DVS/MNIST_DVS/train_cache_{}'.format(step)),
                                      transform=train_transform, num_copies=3)
    test_dataset = DiskCachedDataset(test_dataset,
                                     cache_path=os.path.join(DATA_DIR, 'DVS/MNIST_DVS/test_cache_{}'.format(step)),
                                     transform=test_transform, num_copies=3)

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=batch_size,
        pin_memory=True, drop_last=False, num_workers=8,
        shuffle=True,
    )

    test_loader = torch.utils.data.DataLoader(
        test_dataset, batch_size=batch_size,
        pin_memory=True, drop_last=False, num_workers=4,
        shuffle=False,
    )

    return train_loader, test_loader, None, None


def get_fashion_data(batch_size, num_workers=8, same_da=False, root=DATA_DIR, **kwargs):
    """
    获取fashion MNIST数据
    http://arxiv.org/abs/1708.07747
    :param batch_size: batch size
    :param same_da: 为训练集使用测试集的增广方法
    :param kwargs:
    :return: (train loader, test loader, mixup_active, mixup_fn)
    """
    train_transform = transforms.Compose([transforms.RandomCrop(28, padding=4),
                                          transforms.RandomHorizontalFlip(),
                                          transforms.RandomRotation(10),
                                          transforms.ToTensor()])
    test_transform = transforms.Compose([transforms.ToTensor()])

    train_datasets = datasets.FashionMNIST(
        root=root, train=True, transform=test_transform if same_da else train_transform, download=True)
    test_datasets = datasets.FashionMNIST(
        root=root, train=False, transform=test_transform, download=True)

    train_loader = torch.utils.data.DataLoader(
        train_datasets, batch_size=batch_size,
        pin_memory=True, drop_last=True, shuffle=True, num_workers=num_workers
    )

    test_loader = torch.utils.data.DataLoader(
        test_datasets, batch_size=batch_size,
        pin_memory=True, drop_last=False, num_workers=num_workers
    )

    return train_loader, test_loader, False, None


def get_cifar10_data(batch_size, num_workers=8, same_da=False, root=DATA_DIR, **kwargs):
    """
    获取CIFAR10数据
     https://www.cs.toronto.edu/~kriz/cifar.html
    :param batch_size: batch size
    :param kwargs:
    :return: (train loader, test loader, mixup_active, mixup_fn)
    """
    train_datasets, _ = build_dataset(True, 32, 'CIFAR10', root, same_da)
    test_datasets, _ = build_dataset(False, 32, 'CIFAR10', root, same_da)

    train_loader = torch.utils.data.DataLoader(
        train_datasets, batch_size=batch_size,
        pin_memory=True, drop_last=True, shuffle=True,
        num_workers=num_workers
    )

    test_loader = torch.utils.data.DataLoader(
        test_datasets, batch_size=batch_size,
        pin_memory=True, drop_last=False,
        num_workers=num_workers
    )
    return train_loader, test_loader, None, None


class UrbanSound8KDataset(torch.utils.data.Dataset):
    def __init__(self, file_path, class_names, modality, visual_transform=None, audio_transform=None):
        """
        Args:
            file_path (str): 数据集根目录路径
            class_names (list): 类别名称列表
            transform (callable, optional): 变换操作（如数据增强）
        """
        self.file_path = file_path
        self.class_names = class_names
        self.visual_transform = visual_transform
        self.audio_transform = audio_transform
        self.modality = modality
        self.data = []
        self.targets = []

        self.file_path = os.path.join(self.file_path, "Dataset_v3_vision/")

        for path, dirs, files in os.walk(self.file_path):
            dirs.sort()
            for file in files:
                if file.endswith("jpg"):
                    self.data.append(path + "/" + file)
                    label_number = class_names[os.path.basename(path)]
                    self.targets.append(label_number)

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        file_path, label = self.data[idx], self.targets[idx]

        visual_context = PIL.Image.open(file_path).convert("RGB")
        visual_context = self.visual_transform(visual_context)

        ###    -------一种读取方法, same as LinYue Guo-----
        # waveform, sample_rate = torchaudio.load(file_path)
        # transform = torchaudio.transforms.MelSpectrogram(
        #     sample_rate=sample_rate,
        #     n_mels=64,
        #     f_max=8000
        # )
        # mel_spec = transform(waveform)
        # context = torchaudio.transforms.AmplitudeToDB()(mel_spec)  # (1, 64, 1921)

        ### ----------------另一种读取方法---------------
        audio_file_path = file_path.replace("Dataset_v3_vision", "Dataset_v3_sound").replace(".jpg", ".wav")

        waveform, sample_rate = torchaudio.load(audio_file_path, normalize=True)
        waveform = torchaudio.functional.resample(waveform, orig_freq=sample_rate, new_freq=22050)
        waveform = torch.clamp(waveform, -1, 1)

        stft_transforms = torchaudio.transforms.Spectrogram(n_fft=512, hop_length=353, power=None, pad_mode='constant')
        spectrogram = stft_transforms(waveform)
        spectrogram = torch.log(torch.abs(spectrogram) + 1e-7)

        audio_context = PIL.Image.fromarray(spectrogram.squeeze().numpy())  # (249, 257)
        audio_context = self.audio_transform(audio_context)

        if self.modality == "visual":
            return visual_context, label
        elif self.modality == "audio":
            return audio_context, label
        elif self.modality == "audio-visual":
            return (audio_context, visual_context), label


def get_UrbanSound8K_data(batch_size, num_workers=8, same_da=False, root=DATA_DIR, **kwargs):
    """
    获取UrbanSound8K数据
    :param batch_size: batch size
    :param kwargs:
    :return: (train loader, test loader, mixup_active, mixup_fn)
    """
    size = 128
    portion = 0.7
    modality = kwargs['modality']
    args = kwargs['args']

    # 数据集类别，从"air_conditioner"到"street_music"
    class_names = {
        'air_conditioner': 0, 'car_horn': 1, 'children_playing': 2, 'dog_bark': 3, 'drilling': 4,
        'engine_idling': 5, 'gun_shot': 6, 'jackhammer': 7, 'siren': 8, 'street_music': 9
    }

    file_path = os.path.join(root, "UrbanSound8K-AV")

    visual_train_transform = transforms.Compose([
        transforms.RandomResizedCrop(size),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),  # ImageNet标准化
    ])

    visual_test_transform = transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(size),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    audio_train_transform = transforms.Compose([
        transforms.Resize((size, size)),  # 将频谱图像调整到224x224
        transforms.ToTensor(),
    ])

    audio_test_transform = transforms.Compose([
        transforms.Resize((size, size)),  # 将频谱图像调整到224x224
        transforms.ToTensor(),
    ])

    # 创建数据集实例，传入不同的transform
    train_dataset = UrbanSound8KDataset(file_path, class_names, visual_transform=visual_train_transform,
                                        audio_transform=audio_train_transform, modality=modality)
    test_dataset = UrbanSound8KDataset(file_path, class_names, visual_transform=visual_test_transform,
                                       audio_transform=audio_test_transform, modality=modality)

    indices_train = []
    indices_test = []

    # 统计每个类别的样本数
    class_counts = [0 for i in range(10)]
    for label in train_dataset.targets:
        class_counts[label] += 1
    cnt_now = 0

    # 按比例划分训练集和测试集
    for i in range(10):
        indices_train.extend(list(range(cnt_now, round(cnt_now + class_counts[i] * portion))))
        indices_test.extend(list(range(round(cnt_now + class_counts[i] * portion), cnt_now + class_counts[i])))
        cnt_now += class_counts[i]

    DIR = '/home/hexiang/data/'

    train_dataset = DiskCachedDataset(train_dataset,
                                      cache_path=os.path.join(DIR, 'UrbanSound8K-AV/{}/train_cache_{}'.format(modality,
                                                                                                              args.step)),
                                      transform=None, num_copies=3)

    test_dataset = DiskCachedDataset(test_dataset,
                                     cache_path=os.path.join(DIR, 'UrbanSound8K-AV/{}/test_cache_{}'.format(modality,
                                                                                                            args.step)),
                                     transform=None, num_copies=3)

    # 使用SubsetRandomSampler来创建训练和测试的DataLoader
    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=batch_size,
        sampler=torch.utils.data.sampler.SubsetRandomSampler(indices_train),
        pin_memory=True, drop_last=True, num_workers=8
    )

    test_loader = torch.utils.data.DataLoader(
        test_dataset, batch_size=batch_size,
        sampler=torch.utils.data.sampler.SubsetRandomSampler(indices_test),
        pin_memory=True, drop_last=False, num_workers=8
    )

    return train_loader, test_loader, None, None


def get_AVmnistdvs_data(batch_size, step, dvs_da=False, **kwargs):
    """
    获取MNIST-DVS数据
    http://journal.frontiersin.org/Article/10.3389/fnins.2015.00437/abstract
    :param batch_size: batch size
    :param step: 仿真步长
    :param kwargs:
    :return: (train loader, test loader, mixup_active, mixup_fn)
    """

    # sensor_size = tonic.datasets.MNISTDVS.sensor_size
    sensor_size = (28, 28, 2)
    size = kwargs['size'] if 'size' in kwargs else 28
    portion = 0.9

    modality = kwargs['modality']
    args = kwargs['args']

    indices_train = []
    indices_test = []

    # 统计每个类别的样本数
    class_counts_train = [500 for i in range(10)]
    cnt_now_train = 0

    class_counts_test = [500 for i in range(10)]
    cnt_now_test = 0

    # 按比例划分训练集和测试集
    for i in range(10):
        indices_train.extend(list(range(cnt_now_train, round(cnt_now_train + class_counts_train[i]))))
        indices_test.extend(list(range(cnt_now_test, round(cnt_now_test + class_counts_test[i]))))
        cnt_now_train += class_counts_train[i]
        cnt_now_test += class_counts_test[i]

    # ------------visual-----------#
    train_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        # tonic.transforms.DropEvent(p=0.1),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step),
    ])
    test_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step),
    ])

    visual_train_dataset = tonic.datasets.MNISTDVS(os.path.join(DATA_DIR, 'DVS/MNIST_DVS/'),
                                                   transform=train_transform, train=True)

    visual_test_dataset = tonic.datasets.MNISTDVS(os.path.join(DATA_DIR, 'DVS/MNIST_DVS/'),
                                                  transform=test_transform, train=False)

    # 2. 合并训练集和测试集
    full_dataset = ConcatDataset([visual_train_dataset, visual_test_dataset])

    # 3. 按类别分组所有样本的索引
    class_indices = defaultdict(list)

    for idx in range(len(full_dataset)):
        # 获取样本和标签
        _, label = full_dataset[idx]
        class_indices[label].append(idx)

    # 检查每个类别是否有 1000 个样本
    for label, indices in class_indices.items():
        if len(indices) != 1000:
            raise ValueError(f"类别 {label} 的样本数量为 {len(indices)}，预期为 1000。")

    # 4. 对每个类别的索引进行打乱并划分
    train_indices = []
    test_indices = []

    for label, indices in class_indices.items():
        shuffled = indices.copy()
        random.shuffle(shuffled)
        # 前 500 个样本作为训练集
        train_indices.extend(shuffled[:500])
        # 后 500 个样本作为测试集
        test_indices.extend(shuffled[500:1000])

    # 5. 创建新的训练集和测试集
    visual_train_dataset = Subset(full_dataset, train_indices)
    visual_test_dataset = Subset(full_dataset, test_indices)

    MNIST_MEAN = 0.1307
    MNIST_STD = 0.3081
    # cached_transform = transforms.Compose([
    #     lambda x: torch.tensor(x, dtype=torch.float),
    #     lambda x: F.interpolate(x, size=[size, size], mode='bilinear', align_corners=True),
    # ])

    train_transform = transforms.Compose([lambda x: torch.tensor(x, dtype=torch.float),
                                          lambda x: F.interpolate(x, size=[size, size], mode='bilinear',
                                                                  align_corners=True),
                                          transforms.RandomCrop(28, padding=4),
                                          transforms.Normalize((MNIST_MEAN,), (MNIST_STD,))])
    test_transform = transforms.Compose([lambda x: torch.tensor(x, dtype=torch.float),
                                         lambda x: F.interpolate(x, size=[size, size], mode='bilinear',
                                                                 align_corners=True),
                                         transforms.Normalize((MNIST_MEAN,), (MNIST_STD,))])

    visual_train_dataset = DiskCachedDataset(visual_train_dataset,
                                             cache_path=os.path.join("/home/hexiang/",
                                                                     'DVS/MNIST_DVS/train_cache_{}'.format(step)),
                                             transform=train_transform, num_copies=3)

    visual_test_dataset = DiskCachedDataset(visual_test_dataset,
                                            cache_path=os.path.join("/home/hexiang/",
                                                                    'DVS/MNIST_DVS/test_cache_{}'.format(step)),
                                            transform=test_transform, num_copies=3)

    # 使用SubsetRandomSampler来创建训练和测试的DataLoader
    train_loader = torch.utils.data.DataLoader(
        visual_train_dataset, batch_size=batch_size,
        sampler=torch.utils.data.sampler.SubsetRandomSampler(indices_train),
        pin_memory=True, drop_last=True, num_workers=4
    )

    test_loader = torch.utils.data.DataLoader(
        visual_test_dataset, batch_size=batch_size,
        sampler=torch.utils.data.sampler.SubsetRandomSampler(indices_test),
        pin_memory=True, drop_last=False, num_workers=4
    )

    if modality == "visual":
        return train_loader, test_loader, None, None

    # ------------audio-----------#
    sensor_size = tonic.datasets.NTIDIGITS.sensor_size
    train_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        # tonic.transforms.DropEvent(p=0.1),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step),
    ])

    test_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step),
    ])

    audio_train_dataset = tonic.datasets.NTIDIGITS(os.path.join(DATA_DIR, 'DVS/'),
                                                   transform=train_transform, train=True)

    audio_test_dataset = tonic.datasets.NTIDIGITS(os.path.join(DATA_DIR, 'DVS/'),
                                                  transform=test_transform, train=False)

    cached_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        lambda x: x.view(x.size(0), x.size(1), 8, 8),  # 重新塑形到 (B, T, 1 H, W)
        lambda x: F.interpolate(x, size=(28, 28), mode='bilinear', align_corners=False)  # 插值到 (B, T, 1, 26, 26)
    ])

    audio_train_dataset = DiskCachedDataset(audio_train_dataset,
                                            cache_path=os.path.join("/home/hexiang/",
                                                                    'DVS/NTIDIGITS/train_cache_{}'.format(step)),
                                            transform=cached_transform, num_copies=3)

    audio_test_dataset = DiskCachedDataset(audio_test_dataset,
                                           cache_path=os.path.join("/home/hexiang/",
                                                                   'DVS/NTIDIGITS/test_cache_{}'.format(step)),
                                           transform=cached_transform, num_copies=3)

    spec_index = [[] for i in range(10)]
    for idx, (spec, label) in enumerate(audio_train_dataset):
        label = label.decode('utf-8')
        if label[-1] == 'z':
            label = 0
        else:
            label = int(label[-1])
        spec_index[label].append(spec.clone().detach())

    for i in range(10):
        spec_index[i] = torch.stack(spec_index[i], 0)
        repeat_factor = 500 // len(spec_index[i]) + 1
        spec_index[i] = spec_index[i].repeat(repeat_factor, 1, 1, 1, 1)[:500]

    audio_train_data = torch.cat(spec_index, dim=0)
    train_labels = [i for i in range(10) for _ in range(500)]

    audio_train_dataset = MyDataSet(data=audio_train_data, label=train_labels)

    spec_index = [[] for i in range(10)]
    for idx, (spec, label) in enumerate(audio_test_dataset):
        label = label.decode('utf-8')
        if label[-1] == 'z':
            label = 0
        else:
            label = int(label[-1])
        spec_index[label].append(spec.clone().detach())

    for i in range(10):
        spec_index[i] = torch.stack(spec_index[i], 0)
        repeat_factor = 500 // len(spec_index[i]) + 1
        spec_index[i] = spec_index[i].repeat(repeat_factor, 1, 1, 1, 1)[:500]

    audio_test_data = torch.cat(spec_index, dim=0)
    test_labels = [i for i in range(10) for _ in range(500)]

    audio_test_dataset = MyDataSet(data=audio_test_data, label=test_labels)

    train_loader = torch.utils.data.DataLoader(
        audio_train_dataset, batch_size=batch_size,
        sampler=torch.utils.data.sampler.SubsetRandomSampler(indices_train),
        pin_memory=True, drop_last=True, num_workers=4
    )

    test_loader = torch.utils.data.DataLoader(
        audio_test_dataset, batch_size=batch_size,
        sampler=torch.utils.data.sampler.SubsetRandomSampler(indices_test),
        pin_memory=True, drop_last=False, num_workers=4
    )

    if modality == "audio":
        return train_loader, test_loader, None, None

    # --------audio-visual---- 测试集不够就扩充#
    AV_data = []
    AV_label = []

    for idx, (audio, visual) in enumerate(zip(audio_train_dataset, visual_train_dataset)):
        audio_data, audio_label = audio
        visual_data, visual_label = visual
        AV_data.append((audio_data, visual_data))
        AV_label.append(audio_label)

    train_dataset = MyAVDataSet(data=AV_data, label=AV_label)

    AV_data = []
    AV_label = []

    for idx, (audio, visual) in enumerate(zip(audio_test_dataset, visual_test_dataset)):
        audio_data, audio_label = audio
        visual_data, visual_label = visual
        AV_data.append((audio_data, visual_data))
        AV_label.append(audio_label)

    test_dataset = MyAVDataSet(data=AV_data, label=AV_label)

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=batch_size,
        sampler=torch.utils.data.sampler.SubsetRandomSampler(indices_train),
        pin_memory=True, drop_last=True, num_workers=4
    )

    test_loader = torch.utils.data.DataLoader(
        test_dataset, batch_size=batch_size,
        sampler=torch.utils.data.sampler.SubsetRandomSampler(indices_test),
        pin_memory=True, drop_last=False, num_workers=4
    )

    if modality == "audio-visual":
        return train_loader, test_loader, None, None


class CREMADDataset(torch.utils.data.Dataset):
    def __init__(self, file_path, class_names, modality, train, visual_transform=None, audio_transform=None):
        """
        Args:
            file_path (str): 数据集根目录路径
            class_names (list): 类别名称列表
            transform (callable, optional): 变换操作（如数据增强）
        """
        self.file_path = file_path
        self.class_names = class_names
        self.visual_transform = visual_transform
        self.audio_transform = audio_transform
        self.modality = modality
        self.train = train
        self.data = []
        self.targets = []

        if self.train:
            self.file_path = os.path.join(self.file_path, "train/visual")
        else:
            self.file_path = os.path.join(self.file_path, "test/visual")

        for path, dirs, files in os.walk(self.file_path):
            dirs.sort()
            for file in files:
                if file.endswith("jpg"):
                    self.data.append(path + "/" + file)
                    label_number = class_names[os.path.basename(path)]
                    self.targets.append(label_number)

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        file_path, label = self.data[idx], self.targets[idx]

        visual_context = PIL.Image.open(file_path).convert("RGB")  # 读取相对花时间, 0.01 or 0.007 per sample
        visual_context = self.visual_transform(visual_context)

        ###    -------一种读取方法, same as LinYue Guo-----
        # waveform, sample_rate = torchaudio.load(file_path)
        # transform = torchaudio.transforms.MelSpectrogram(
        #     sample_rate=sample_rate,
        #     n_mels=64,
        #     f_max=8000
        # )
        # mel_spec = transform(waveform)
        # context = torchaudio.transforms.AmplitudeToDB()(mel_spec)  # (1, 64, 1921)

        ### ----------------另一种读取方法---------------
        audio_file_path = file_path.replace("visual", "audio").replace(".jpg", ".wav")

        waveform, sample_rate = torchaudio.load(audio_file_path, normalize=True)

        waveform = torchaudio.functional.resample(waveform, orig_freq=sample_rate,
                                                  new_freq=22050)  # 采样相对花时间, 0.02 per sample
        waveform = waveform.repeat(1, 3)[:, :22050 * 3]
        waveform = torch.clamp(waveform, -1, 1)

        stft_transforms = torchaudio.transforms.Spectrogram(n_fft=512, hop_length=353, power=None, pad_mode='constant')
        spectrogram = stft_transforms(waveform)
        spectrogram = torch.log(torch.abs(spectrogram) + 1e-7)

        audio_context = PIL.Image.fromarray(spectrogram.squeeze().numpy())  # (249, 257)
        audio_context = self.audio_transform(audio_context)

        if self.modality == "visual":
            return visual_context, label
        elif self.modality == "audio":
            return audio_context, label
        elif self.modality == "audio-visual":
            return (audio_context, visual_context), label


def get_CREMAD_data(batch_size, num_workers=8, same_da=False, root=DATA_DIR, **kwargs):
    """
    获取CREMAD数据
    :param batch_size: batch size
    :param kwargs:
    :return: (train loader, test loader, mixup_active, mixup_fn)
    """
    size = 224
    modality = kwargs['modality']
    args = kwargs['args']

    # 数据集类别，从"air_conditioner"到"street_music"
    class_names = {
        'ANG': 0, 'DIS': 1, 'FEA': 2, 'HAP': 3, 'NEU': 4, 'SAD': 5
    }

    file_path = os.path.join(root, "CREMA-D/processed_dataset")

    visual_train_transform = transforms.Compose([
        transforms.RandomResizedCrop(size),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),  # ImageNet标准化
    ])

    visual_test_transform = transforms.Compose([
        transforms.Resize((size, size)),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    audio_train_transform = transforms.Compose([
        # transforms.Resize((size, size)),  # 将频谱图像调整到224x224; 为了能够使用collate_fn
        transforms.ToTensor(),
    ])

    audio_test_transform = transforms.Compose([
        # transforms.Resize((size, size)),  # 将频谱图像调整到224x224
        transforms.ToTensor(),
    ])

    # 创建数据集实例，传入不同的transform
    train_dataset = CREMADDataset(file_path, class_names, visual_transform=visual_train_transform,
                                  audio_transform=audio_train_transform, modality=modality, train=True)
    test_dataset = CREMADDataset(file_path, class_names, visual_transform=visual_test_transform,
                                 audio_transform=audio_test_transform, modality=modality, train=False)

    indices_train = []

    indices_test = []

    # 统计每个类别的样本数
    class_counts_train = [1166, 1142, 1138, 1148, 973, 1131]
    cnt_now_train = 0

    class_counts_test = [105, 129, 133, 123, 114, 140]
    cnt_now_test = 0

    # 按比例划分训练集和测试集
    for i in range(6):
        indices_train.extend(list(range(cnt_now_train, round(cnt_now_train + class_counts_train[i]))))
        cnt_now_train += class_counts_train[i]

        indices_test.extend(list(range(cnt_now_test, round(cnt_now_test + class_counts_test[i]))))
        cnt_now_test += class_counts_test[i]

    # train_dataset = DiskCachedDataset(train_dataset,
    #                                   cache_path=os.path.join(DATA_DIR, 'CREMA-D/{}/train_cache_{}'.format(modality, args.step)),
    #                                   transform=None, num_copies=3)
    #
    # test_dataset = DiskCachedDataset(test_dataset,
    #                                   cache_path=os.path.join(DATA_DIR, 'CREMA-D/{}/test_cache_{}'.format(modality, args.step)),
    #                                   transform=None, num_copies=3)

    # 使用SubsetRandomSampler来创建训练和测试的DataLoader
    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=batch_size, shuffle=True,
        pin_memory=True, drop_last=True, num_workers=32
    )

    test_loader = torch.utils.data.DataLoader(
        test_dataset, batch_size=batch_size, shuffle=False,
        pin_memory=True, drop_last=False, num_workers=32
    )

    return train_loader, test_loader, None, None


class KineticSoundAudioDataset(torch.utils.data.Dataset):
    def __init__(self, file_path, class_names, train):
        """
        Args:
            file_path (str): 数据集根目录路径
            class_names (list): 类别名称列表
            transform (callable, optional): 变换操作（如数据增强）
        """

        if train:
            self.mode = "train"
        else:
            self.mode = "test"
        classes = []
        self.av_files = []
        self.name2class = {}
        self.class2name = {}

        # 数据根路径
        self.data_root = file_path

        # 训练和测试数据集路径
        self.video_feature_path = os.path.join(self.data_root, self.mode, 'video/')
        self.audio_feature_path = os.path.join(self.data_root, self.mode, 'audio/')
        self.train_txt = os.path.join(self.data_root, 'my_train.txt')
        self.test_txt = os.path.join(self.data_root, 'my_test.txt')

        # 选择相应的 CSV 文件
        csv_file = self.train_txt if self.mode == 'train' else self.test_txt

        # 读取类别信息
        self.class2name = {v: k for k, v in class_names.items()}
        with open(csv_file) as f2:
            csv_reader = csv.reader(f2)
            for item in csv_reader:
                vid_start_end = item[0]  # 视频音频标识符
                if self.mode == "test":
                    vid_start_end = vid_start_end[:11]
                label_id = int(item[2])  # 类别名称

                # if os.path.exists(audio_path) and os.path.exists(visual_path):
                self.av_files.append(vid_start_end)
                self.name2class[vid_start_end] = label_id  # 关联类别

    def __len__(self):
        return len(self.av_files)

    def __getitem__(self, idx):

        file_path = self.av_files[idx]
        label = self.name2class[file_path]

        # 音频路径
        audio_file_path = os.path.join(self.audio_feature_path, self.class2name[label], file_path + '.wav')

        waveform, sample_rate = torchaudio.load(audio_file_path, normalize=True)
        waveform = torchaudio.functional.resample(waveform, orig_freq=sample_rate, new_freq=22050)
        waveform = waveform.repeat(1, 3)[:, :22050 * 3]
        waveform = torch.clamp(waveform, -1, 1)

        stft_transforms = torchaudio.transforms.Spectrogram(n_fft=512, hop_length=353, power=None, pad_mode='constant')
        spectrogram = stft_transforms(waveform)
        spectrogram = torch.log(torch.abs(spectrogram) + 1e-7)

        return spectrogram, label


class KineticSoundVisualDataset(torch.utils.data.Dataset):
    def __init__(self, file_path, class_names, train):
        """
        Args:
            file_path (str): 数据集根目录路径
            class_names (list): 类别名称列表
            transform (callable, optional): 变换操作（如数据增强）
        """

        if train:
            self.mode = "train"
        else:
            self.mode = "test"
        classes = []
        self.av_files = []
        self.name2class = {}
        self.class2name = {}

        # 数据根路径
        self.data_root = file_path

        # 训练和测试数据集路径
        self.video_feature_path = os.path.join(self.data_root, self.mode, 'video/')
        self.audio_feature_path = os.path.join(self.data_root, self.mode, 'audio/')
        self.train_txt = os.path.join(self.data_root, 'my_train.txt')
        self.test_txt = os.path.join(self.data_root, 'my_test.txt')

        # 选择相应的 CSV 文件
        csv_file = self.train_txt if self.mode == 'train' else self.test_txt

        # 读取类别信息
        self.class2name = {v: k for k, v in class_names.items()}
        with open(csv_file) as f2:
            csv_reader = csv.reader(f2)
            for item in csv_reader:
                vid_start_end = item[0]  # 视频音频标识符
                if self.mode == "test":
                    vid_start_end = vid_start_end[:11]
                label_id = int(item[2])  # 类别名称

                # if os.path.exists(audio_path) and os.path.exists(visual_path):
                self.av_files.append(vid_start_end)
                self.name2class[vid_start_end] = label_id  # 关联类别

    def __len__(self):
        return len(self.av_files)

    def __getitem__(self, idx):

        file_path = self.av_files[idx]
        label = self.name2class[file_path]

        visual_path = os.path.join(self.video_feature_path, self.class2name[label], file_path)

        return visual_path, label


class KineticSoundVisualReloadDataset(torch.utils.data.Dataset):
    def __init__(self, base_dataset):
        self.base = base_dataset  # 保存已有实例

    def __len__(self):
        return len(self.base)

    def __getitem__(self, index):
        visual_data, label_v = self.base[index]  # 直接调用已有实例
        visual_data = torch.stack(visual_data, dim=0).permute(1, 0, 2, 3)

        return visual_data, label_v


class KineticSoundAudioVisualDataset(torch.utils.data.Dataset):
    def __init__(self, audio_dataset, visual_dataset):
        """
        :param audio_dataset: AudioDataset 实例
        :param visual_dataset: VideoDataset 实例
        """
        self.audio_dataset = audio_dataset
        self.visual_dataset = visual_dataset

    def __len__(self):
        return len(self.audio_dataset)

    def __getitem__(self, idx):
        audio_data, label_a = self.audio_dataset[idx]
        visual_data, label_v = self.visual_dataset[idx]
        visual_data = torch.stack(visual_data, dim=0).permute(1, 0, 2, 3)

        assert label_a == label_v, "音频和视频的label不匹配，检查数据排序/对齐"

        return (audio_data, visual_data), label_a


def get_KineticSound_data(batch_size, step, num_workers=8, same_da=False, root=DATA_DIR, **kwargs):
    """
    获取CREMAD数据
    :param batch_size: batch size
    :param kwargs:
    :return: (train loader, test loader, mixup_active, mixup_fn)
    """
    size = 224
    modality = kwargs['modality']
    args = kwargs['args']

    class_names = {'blowing nose': 0, 'blowing out candles': 1, 'bowling': 2, 'chopping wood': 3,
                   'dribbling basketball': 4,
                   'laughing': 5, 'mowing lawn': 6, 'playing accordion': 7, 'playing bagpipes': 8,
                   'playing bass guitar': 9,
                   'playing clarinet': 10, 'playing drums': 11, 'playing guitar': 12, 'playing harmonica': 13,
                   'playing keyboard': 14,
                   'playing organ': 15, 'playing piano': 16, 'playing saxophone': 17, 'playing trombone': 18,
                   'playing trumpet': 19,
                   'playing violin': 20, 'playing xylophone': 21, 'ripping paper': 22, 'shoveling snow': 23,
                   'shuffling cards': 24,
                   'singing': 25, 'stomping grapes': 26, 'tap dancing': 27, 'tapping guitar': 28, 'tapping pen': 29,
                   'tickling': 30}

    file_path = '/mnt/home/hexiang/kinetics_sound/'

    visual_train_transform = transforms.Compose([
        lambda x: PIL.Image.fromarray(x),  # (257, 188)
        transforms.RandomResizedCrop(size),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),  # ImageNet标准化
    ])

    visual_test_transform = transforms.Compose([
        lambda x: PIL.Image.fromarray(x),  # (257, 188)
        transforms.Resize((size, size)),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    audio_train_transform = transforms.Compose([
        lambda x: PIL.Image.fromarray(np.squeeze(x)),  # (257, 188)
        transforms.ToTensor(),
    ])

    audio_test_transform = transforms.Compose([
        lambda x: PIL.Image.fromarray(np.squeeze(x)),  # (257, 188)
        transforms.ToTensor(),
    ])

    # 创建数据集实例，传入不同的transform
    audio_train_dataset = KineticSoundAudioDataset(file_path, class_names, train=True)
    audio_train_dataset = DiskCachedDataset(audio_train_dataset, cache_path=os.path.join("/mnt/home/hexiang/",
                                                                                         'KineticSound/audio/train_cache_{}'.format(
                                                                                             step)),
                                            transform=audio_train_transform, num_copies=1)
    audio_test_dataset = KineticSoundAudioDataset(file_path, class_names, train=False)
    audio_test_dataset = DiskCachedDataset(audio_test_dataset, cache_path=os.path.join("/mnt/home/hexiang/",
                                                                                       'KineticSound/audio/test_cache_{}'.format(
                                                                                           step)),
                                           transform=audio_test_transform, num_copies=1)

    visual_train_dataset = KineticSoundVisualDataset(file_path, class_names, train=True)
    visual_train_dataset = RandomChoiceDiskCachedDataset(visual_train_dataset,
                                                         cache_path=os.path.join("/mnt/home/hexiang/",
                                                                                 'KineticSound/visual/train_cache_{}'.format(
                                                                                     step)),
                                                         transform=visual_train_transform, num_copies=1)
    visual_test_dataset = KineticSoundVisualDataset(file_path, class_names, train=False)
    visual_test_dataset = RandomChoiceDiskCachedDataset(visual_test_dataset,
                                                        cache_path=os.path.join("/mnt/home/hexiang/",
                                                                                'KineticSound/visual/test_cache_{}'.format(
                                                                                    step)),
                                                        transform=visual_test_transform, num_copies=1)

    if modality == "audio":
        train_dataset = audio_train_dataset
        test_dataset = audio_test_dataset
    elif modality == "visual":
        train_dataset = KineticSoundVisualReloadDataset(visual_train_dataset)
        test_dataset = KineticSoundVisualReloadDataset(visual_test_dataset)
    elif modality == "audio-visual":
        train_dataset = KineticSoundAudioVisualDataset(audio_train_dataset, visual_train_dataset)
        test_dataset = KineticSoundAudioVisualDataset(audio_test_dataset, visual_test_dataset)

    # 使用SubsetRandomSampler来创建训练和测试的DataLoader
    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=batch_size, shuffle=True,
        pin_memory=True, drop_last=True, num_workers=16
    )

    test_loader = torch.utils.data.DataLoader(
        test_dataset, batch_size=batch_size, shuffle=False,
        pin_memory=True, drop_last=False, num_workers=16
    )

    return train_loader, test_loader, None, None


def get_cifar100_data(batch_size, num_workers=8, same_data=False, root=DATA_DIR, *args, **kwargs):
    """
    获取CIFAR100数据
    https://www.cs.toronto.edu/~kriz/cifar.html
    :param batch_size: batch size
    :param kwargs:
    :return: (train loader, test loader, mixup_active, mixup_fn)
    """
    train_datasets, _ = build_dataset(True, 32, 'CIFAR100', root, same_data)
    test_datasets, _ = build_dataset(False, 32, 'CIFAR100', root, same_data)

    train_loader = torch.utils.data.DataLoader(
        train_datasets, batch_size=batch_size,
        pin_memory=True, drop_last=True, shuffle=True, num_workers=num_workers
    )

    test_loader = torch.utils.data.DataLoader(
        test_datasets, batch_size=batch_size,
        pin_memory=True, drop_last=False, num_workers=num_workers
    )
    return train_loader, test_loader, False, None


def get_TinyImageNet_data(batch_size, num_workers=8, same_da=False, root=DATA_DIR, *args, **kwargs):
    size = kwargs["size"] if "size" in kwargs else 224
    train_transform = transforms.Compose([
        transforms.RandomResizedCrop(size),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))
    ])
    test_transform = transforms.Compose([
        transforms.Resize(size * 8 // 7),
        transforms.CenterCrop(size),
        transforms.ToTensor(),
        transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))
    ])
    root = os.path.join(root, 'TinyImageNet')
    train_datasets = TinyImageNet(
        root=root, split="train", transform=test_transform if same_da else train_transform, download=True)
    test_datasets = TinyImageNet(
        root=root, split="val", transform=test_transform, download=True)

    train_loader = torch.utils.data.DataLoader(
        train_datasets, batch_size=batch_size,
        pin_memory=True, drop_last=True, shuffle=True, num_workers=num_workers
    )

    test_loader = torch.utils.data.DataLoader(
        test_datasets, batch_size=batch_size,
        pin_memory=True, drop_last=False, num_workers=num_workers
    )

    return train_loader, test_loader, False, None


def get_imnet_data(args, _logger, data_config, num_aug_splits, root=DATA_DIR, **kwargs):
    """
    获取ImageNet数据集
    http://arxiv.org/abs/1409.0575
    :param args: 其他的参数
    :param _logger: 日志路径
    :param data_config: 增强策略
    :param num_aug_splits: 不同增强策略的数量
    :param kwargs:
    :return: (train loader, test loader, mixup_active, mixup_fn)
    """
    train_dir = os.path.join(root, 'ILSVRC2012/train')
    if not os.path.exists(train_dir):
        _logger.error(
            'Training folder does not exist at: {}'.format(train_dir))
        exit(1)
    dataset_train = ImageDataset(train_dir)
    # collate_fn = None
    # mixup_fn = None
    # mixup_active = args.mixup > 0 or args.cutmix > 0. or args.cutmix_minmax is not None
    # if mixup_active:
    #     mixup_args = dict(
    #         mixup_alpha=args.mixup, cutmix_alpha=args.cutmix, cutmix_minmax=args.cutmix_minmax,
    #         prob=args.mixup_prob, switch_prob=args.mixup_switch_prob, mode=args.mixup_mode,
    #         label_smoothing=args.smoothing, num_classes=args.num_classes)
    #     if args.prefetcher:
    #         # collate conflict (need to support deinterleaving in collate mixup)
    #         assert not num_aug_splits
    #         collate_fn = FastCollateMixup(**mixup_args)
    #     else:
    #         mixup_fn = Mixup(**mixup_args)

    # if num_aug_splits > 1:
    #     dataset_train = AugMixDataset(dataset_train, num_splits=num_aug_splits)

    train_interpolation = args.train_interpolation
    if args.no_aug or not train_interpolation:
        train_interpolation = data_config['interpolation']
    loader_train = create_loader(
        dataset_train,
        input_size=data_config['input_size'],
        batch_size=args.batch_size,
        is_training=True,
        use_prefetcher=args.prefetcher,
        no_aug=args.no_aug,
        # re_prob=args.reprob,
        # re_mode=args.remode,
        # re_count=args.recount,
        # re_split=args.resplit,
        scale=args.scale,
        ratio=args.ratio,
        hflip=args.hflip,
        # vflip=arg,
        color_jitter=args.color_jitter,
        # auto_augment=args.aa,
        num_aug_splits=num_aug_splits,
        interpolation=train_interpolation,
        mean=data_config['mean'],
        std=data_config['std'],
        num_workers=args.workers,
        distributed=args.distributed,
        # collate_fn=collate_fn,
        pin_memory=args.pin_mem,
        use_multi_epochs_loader=args.use_multi_epochs_loader)
    eval_dir = os.path.join(root, 'ILSVRC2012/val')
    if not os.path.isdir(eval_dir):
        eval_dir = os.path.join(root, 'ILSVRC2012/validation')
        if not os.path.isdir(eval_dir):
            _logger.error(
                'Validation folder does not exist at: {}'.format(eval_dir))
            exit(1)
    dataset_eval = ImageDataset(eval_dir)

    loader_eval = create_loader(
        dataset_eval,
        input_size=data_config['input_size'],
        batch_size=args.validation_batch_size_multiplier * args.batch_size,
        is_training=False,
        use_prefetcher=args.prefetcher,
        interpolation=data_config['interpolation'],
        mean=data_config['mean'],
        std=data_config['std'],
        num_workers=args.workers,
        distributed=args.distributed,
        crop_pct=data_config['crop_pct'],
        pin_memory=args.pin_mem,
    )
    return loader_train, loader_eval, False, None


def get_ntidigits_data(batch_size, step, **kwargs):
    """
    获取N-TIDIGITS数据 (tonic 新版本中的下载链接可能挂了，可以参考0.4.0的版本)
    https://www.frontiersin.org/articles/10.3389/fnins.2018.00023/full
    :param batch_size: batch size
    :param step: 仿真步长
    :param kwargs:
    :return: (train loader, test loader, mixup_active, mixup_fn)
    :format: (b,t,c,len) 不同于vision, audio中c为1, 并且没有h,w; 只有len=64
    """
    sensor_size = tonic.datasets.NTIDIGITS.sensor_size
    train_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        # tonic.transforms.DropEvent(p=0.1),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step),
    ])
    test_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step),
    ])

    train_dataset = tonic.datasets.NTIDIGITS(os.path.join(DATA_DIR, 'DVS/'),
                                             transform=train_transform, train=True)

    test_dataset = tonic.datasets.NTIDIGITS(os.path.join(DATA_DIR, 'DVS/'),
                                            transform=test_transform, train=False)
    spec_index = [[] for i in range(10)]
    for idx, (spec, label) in enumerate(train_dataset):
        if label[-1] == 'z':
            label = 0
        else:
            label = int(label[-1])
        spec_index[label].append(torch.tensor(spec))
    for i in range(10):
        spec_index[i] = torch.stack(spec_index[i], 0)
    train_SHD_list = [0] * 11
    for i in range(1, 11):
        train_SHD_list[i] = train_SHD_list[i - 1] + len(spec_index[i - 1])
    train_dataset = MyDataSet(data=torch.cat(spec_index, dim=0),
                              label=None)

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=len(train_dataset),
        sampler=MySampler(torch.arange(0, len(train_dataset)).tolist()),
        pin_memory=True, drop_last=False, num_workers=8
    )

    spec_index = [[] for i in range(10)]
    for idx, (spec, label) in enumerate(test_dataset):
        if label[-1] == 'z':
            label = 0
        else:
            label = int(label[-1])
        spec_index[label].append(torch.tensor(spec))
    for i in range(10):
        spec_index[i] = torch.stack(spec_index[i], 0)
    test_SHD_list = [0] * 11
    for i in range(1, 11):
        test_SHD_list[i] = test_SHD_list[i - 1] + len(spec_index[i - 1])
    test_dataset = MyDataSet(data=torch.cat(spec_index, dim=0),
                             label=None)

    test_loader = torch.utils.data.DataLoader(
        test_dataset, batch_size=len(test_dataset),
        sampler=MySampler(torch.arange(0, len(test_dataset)).tolist()),
        pin_memory=True, drop_last=False, num_workers=8
    )

    return train_loader, train_SHD_list, test_loader, test_SHD_list


def get_dvsg_data(batch_size, step, root=DATA_DIR, **kwargs):
    """
    获取DVS Gesture数据
    DOI: 10.1109/CVPR.2017.781
    :param batch_size: batch size
    :param step: 仿真步长
    :param kwargs:
    :return: (train loader, test loader, mixup_active, mixup_fn)
    """
    sensor_size = tonic.datasets.DVSGesture.sensor_size
    size = kwargs['size'] if 'size' in kwargs else 48

    train_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        # tonic.transforms.DropEvent(p=0.1),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step),
    ])
    test_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step),
    ])

    train_dataset = tonic.datasets.DVSGesture(os.path.join(root, 'DVS/DVSGesture'),
                                              transform=train_transform, train=True)
    test_dataset = tonic.datasets.DVSGesture(os.path.join(root, 'DVS/DVSGesture'),
                                             transform=test_transform, train=False)

    train_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        lambda x: F.interpolate(x, size=[size, size], mode='bilinear', align_corners=True),
        lambda x: dvs_channel_check_expend(x),
        transforms.RandomCrop(size, padding=size // 12),
        # transforms.RandomHorizontalFlip(),
        # transforms.RandomRotation(15)
    ])
    test_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        lambda x: F.interpolate(x, size=[size, size], mode='bilinear', align_corners=True),
        lambda x: dvs_channel_check_expend(x),
    ])
    if 'rand_aug' in kwargs.keys():
        if kwargs['rand_aug'] is True:
            n = kwargs['randaug_n']
            m = kwargs['randaug_m']
            train_transform.transforms.insert(2, RandAugment(m=m, n=n))

    # if 'temporal_flatten' in kwargs.keys():
    #     if kwargs['temporal_flatten'] is True:
    #         train_transform.transforms.insert(-1, lambda x: temporal_flatten(x))
    #         test_transform.transforms.insert(-1, lambda x: temporal_flatten(x))

    train_dataset = DiskCachedDataset(train_dataset,
                                      cache_path=os.path.join(root, 'DVS/DVSGesture/train_cache_{}'.format(step)),
                                      transform=train_transform, num_copies=3)
    test_dataset = DiskCachedDataset(test_dataset,
                                     cache_path=os.path.join(root, 'DVS/DVSGesture/test_cache_{}'.format(step)),
                                     transform=test_transform, num_copies=3)

    mix_up, cut_mix, event_mix, beta, prob, num, num_classes, noise, gaussian_n = unpack_mix_param(kwargs)
    mixup_active = cut_mix | event_mix | mix_up

    if cut_mix:
        train_dataset = CutMix(train_dataset,
                               beta=beta,
                               prob=prob,
                               num_mix=num,
                               num_class=num_classes,
                               noise=noise)

    if event_mix:
        train_dataset = EventMix(train_dataset,
                                 beta=beta,
                                 prob=prob,
                                 num_mix=num,
                                 num_class=num_classes,
                                 noise=noise,
                                 gaussian_n=gaussian_n)
    if mix_up:
        train_dataset = MixUp(train_dataset,
                              beta=beta,
                              prob=prob,
                              num_mix=num,
                              num_class=num_classes,
                              noise=noise)

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=batch_size,
        pin_memory=True, drop_last=True, num_workers=8,
        shuffle=True,
    )

    test_loader = torch.utils.data.DataLoader(
        test_dataset, batch_size=batch_size,
        pin_memory=True, drop_last=False, num_workers=2,
        shuffle=False,
    )

    return train_loader, test_loader, mixup_active, None


def get_bullyingdvs_data(batch_size, step, root=DATA_DIR, **kwargs):
    """
    获取Bullying10K数据
    NeurIPS 2023
    :param batch_size: batch size
    :param step: 仿真步长
    :param kwargs:
    :return:
    """
    size = kwargs['size'] if 'size' in kwargs else 48
    sensor_size = BULLYINGDVS.sensor_size
    train_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        # tonic.transforms.DropEvent(p=0.1),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step), ])
    test_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step), ])
    train_dataset = BULLYINGDVS('/data/datasets/Bullying10k_processed', transform=train_transform)
    # train_dataset = BULLYINGDVS(os.path.join(root, 'DVS/BULLYINGDVS'), transform=train_transform)
    test_dataset = BULLYINGDVS(os.path.join(root, 'DVS/BULLYINGDVS'), transform=test_transform)

    train_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        lambda x: F.interpolate(x, size=[size, size], mode='bilinear', align_corners=True),
        transforms.RandomCrop(size, padding=size // 12),
        transforms.RandomHorizontalFlip(),
        transforms.RandomRotation(15)
    ])
    test_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        lambda x: F.interpolate(x, size=[size, size], mode='bilinear', align_corners=True),
    ])

    if 'rand_aug' in kwargs.keys():
        if kwargs['rand_aug'] is True:
            n = kwargs['randaug_n']
            m = kwargs['randaug_m']
            # print('randaug', m, n)
            train_transform.transforms.insert(2, RandAugment(m=m, n=n))

    train_dataset = DiskCachedDataset(train_dataset,
                                      cache_path=os.path.join(root, 'DVS/BULLYINGDVS/train_cache_{}'.format(step)),
                                      transform=train_transform)
    test_dataset = DiskCachedDataset(test_dataset,
                                     cache_path=os.path.join(root, 'DVS/BULLYINGDVS/test_cache_{}'.format(step)),
                                     transform=test_transform)

    num_train = len(train_dataset)
    num_per_cls = num_train // 10
    indices_train, indices_test = [], []
    portion = kwargs['portion'] if 'portion' in kwargs else .9
    for i in range(10):
        indices_train.extend(
            list(range(i * num_per_cls, round(i * num_per_cls + num_per_cls * portion))))
        indices_test.extend(
            list(range(round(i * num_per_cls + num_per_cls * portion), (i + 1) * num_per_cls)))

    mix_up, cut_mix, event_mix, beta, prob, num, num_classes, noise, gaussian_n = unpack_mix_param(kwargs)
    mixup_active = cut_mix | event_mix | mix_up

    if cut_mix:
        # print('cut_mix', beta, prob, num, num_classes)
        train_dataset = CutMix(train_dataset,
                               beta=beta,
                               prob=prob,
                               num_mix=num,
                               num_class=num_classes,
                               indices=indices_train,
                               noise=noise)

    if event_mix:
        train_dataset = EventMix(train_dataset,
                                 beta=beta,
                                 prob=prob,
                                 num_mix=num,
                                 num_class=num_classes,
                                 indices=indices_train,
                                 noise=noise,
                                 gaussian_n=gaussian_n)

    if mix_up:
        train_dataset = MixUp(train_dataset,
                              beta=beta,
                              prob=prob,
                              num_mix=num,
                              num_class=num_classes,
                              indices=indices_train,
                              noise=noise)

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=batch_size,
        sampler=torch.utils.data.sampler.SubsetRandomSampler(indices_train),
        pin_memory=True, drop_last=True, num_workers=8
    )

    test_loader = torch.utils.data.DataLoader(
        test_dataset, batch_size=batch_size,
        sampler=torch.utils.data.sampler.SubsetRandomSampler(indices_test),
        pin_memory=True, drop_last=False, num_workers=2
    )

    return train_loader, test_loader, mixup_active, None


def get_dvsc10_data(batch_size, step, root=DATA_DIR, **kwargs):
    """
    获取DVS CIFAR10数据
    http://journal.frontiersin.org/article/10.3389/fnins.2017.00309/full
    :param batch_size: batch size
    :param step: 仿真步长
    :param kwargs:
    :return: (train loader, test loader, mixup_active, mixup_fn)
    """
    size = kwargs['size'] if 'size' in kwargs else 48
    sensor_size = tonic.datasets.CIFAR10DVS.sensor_size
    train_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        # tonic.transforms.DropEvent(p=0.1),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step), ])
    test_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step), ])
    train_dataset = tonic.datasets.CIFAR10DVS(os.path.join(root, 'DVS/DVS_Cifar10'), transform=train_transform)
    test_dataset = tonic.datasets.CIFAR10DVS(os.path.join(root, 'DVS/DVS_Cifar10'), transform=test_transform)

    train_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        lambda x: F.interpolate(x, size=[size, size], mode='bilinear', align_corners=True),
        # lambda x: TemporalShift(x, .01),
        # lambda x: drop(x, 0.15),
        # lambda x: ShearX(x, 15),
        # lambda x: ShearY(x, 15),
        # lambda x: TranslateX(x, 0.225),
        # lambda x: TranslateY(x, 0.225),
        # lambda x: Rotate(x, 15),
        # lambda x: CutoutAbs(x, 0.25),
        # lambda x: CutoutTemporal(x, 0.25),
        # lambda x: GaussianBlur(x, 0.5),
        # lambda x: SaltAndPepperNoise(x, 0.1),
        # transforms.Normalize(DVSCIFAR10_MEAN_16, DVSCIFAR10_STD_16),
        transforms.RandomCrop(size, padding=size // 12),
        transforms.RandomHorizontalFlip(),
        transforms.RandomRotation(15)
    ])
    test_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        lambda x: F.interpolate(x, size=[size, size], mode='bilinear', align_corners=True),
    ])

    if 'rand_aug' in kwargs.keys():
        if kwargs['rand_aug'] is True:
            n = kwargs['randaug_n']
            m = kwargs['randaug_m']
            # print('randaug', m, n)
            train_transform.transforms.insert(2, RandAugment(m=m, n=n))

    # if 'temporal_flatten' in kwargs.keys():
    #     if kwargs['temporal_flatten'] is True:
    #         train_transform.transforms.insert(-1, lambda x: temporal_flatten(x))
    #         test_transform.transforms.insert(-1, lambda x: temporal_flatten(x))

    train_dataset = DiskCachedDataset(train_dataset,
                                      cache_path=os.path.join(root, 'DVS/DVS_Cifar10/train_cache_{}'.format(step)),
                                      transform=train_transform)
    test_dataset = DiskCachedDataset(test_dataset,
                                     cache_path=os.path.join(root, 'DVS/DVS_Cifar10/test_cache_{}'.format(step)),
                                     transform=test_transform)

    num_train = len(train_dataset)
    num_per_cls = num_train // 10
    indices_train, indices_test = [], []
    portion = kwargs['portion'] if 'portion' in kwargs else .9
    for i in range(10):
        indices_train.extend(
            list(range(i * num_per_cls, round(i * num_per_cls + num_per_cls * portion))))
        indices_test.extend(
            list(range(round(i * num_per_cls + num_per_cls * portion), (i + 1) * num_per_cls)))

    mix_up, cut_mix, event_mix, beta, prob, num, num_classes, noise, gaussian_n = unpack_mix_param(kwargs)
    mixup_active = cut_mix | event_mix | mix_up

    if cut_mix:
        # print('cut_mix', beta, prob, num, num_classes)
        train_dataset = CutMix(train_dataset,
                               beta=beta,
                               prob=prob,
                               num_mix=num,
                               num_class=num_classes,
                               indices=indices_train,
                               noise=noise)

    if event_mix:
        train_dataset = EventMix(train_dataset,
                                 beta=beta,
                                 prob=prob,
                                 num_mix=num,
                                 num_class=num_classes,
                                 indices=indices_train,
                                 noise=noise,
                                 gaussian_n=gaussian_n)

    if mix_up:
        train_dataset = MixUp(train_dataset,
                              beta=beta,
                              prob=prob,
                              num_mix=num,
                              num_class=num_classes,
                              indices=indices_train,
                              noise=noise)

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=batch_size,
        sampler=torch.utils.data.sampler.SubsetRandomSampler(indices_train),
        pin_memory=True, drop_last=True, num_workers=8
    )

    test_loader = torch.utils.data.DataLoader(
        test_dataset, batch_size=batch_size,
        sampler=torch.utils.data.sampler.SubsetRandomSampler(indices_test),
        pin_memory=True, drop_last=False, num_workers=2
    )

    return train_loader, test_loader, mixup_active, None


def get_NCALTECH101_data(batch_size, step, root=DATA_DIR, **kwargs):
    """
    获取NCaltech101数据
    http://journal.frontiersin.org/Article/10.3389/fnins.2015.00437/abstract
    :param batch_size: batch size
    :param step: 仿真步长
    :param kwargs:
    :return: (train loader, test loader, mixup_active, mixup_fn)
    """
    sensor_size = tonic.datasets.NCALTECH101.sensor_size
    cls_count = tonic.datasets.NCALTECH101.cls_count
    dataset_length = tonic.datasets.NCALTECH101.length
    portion = kwargs['portion'] if 'portion' in kwargs else .9
    size = kwargs['size'] if 'size' in kwargs else 48
    # print('portion', portion)
    train_sample_weight = []
    train_sample_index = []
    train_count = 0
    test_sample_index = []
    idx_begin = 0
    for count in cls_count:
        sample_weight = dataset_length / count
        train_sample = round(portion * count)
        test_sample = count - train_sample
        train_count += train_sample
        train_sample_weight.extend(
            [sample_weight] * train_sample
        )
        train_sample_weight.extend(
            [0.] * test_sample
        )
        train_sample_index.extend(
            list((range(idx_begin, idx_begin + train_sample)))
        )
        test_sample_index.extend(
            list(range(idx_begin + train_sample, idx_begin + train_sample + test_sample))
        )
        idx_begin += count

    train_sampler = torch.utils.data.sampler.WeightedRandomSampler(train_sample_weight, train_count)
    test_sampler = torch.utils.data.sampler.SubsetRandomSampler(test_sample_index)

    train_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        # tonic.transforms.DropEvent(p=0.1),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step), ])
    test_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step), ])

    train_dataset = tonic.datasets.NCALTECH101(os.path.join(root, 'DVS/NCALTECH101'), transform=train_transform)
    test_dataset = tonic.datasets.NCALTECH101(os.path.join(root, 'DVS/NCALTECH101'), transform=test_transform)

    train_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        # lambda x: print(x.shape),
        lambda x: F.interpolate(x, size=[size, size], mode='bilinear', align_corners=True),
        # transforms.RandomCrop(size, padding=size // 12),
        # transforms.RandomHorizontalFlip(),
        # transforms.RandomRotation(15)
    ])
    test_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        lambda x: F.interpolate(x, size=[size, size], mode='bilinear', align_corners=True),
        # lambda x: temporal_flatten(x),
    ])
    if 'rand_aug' in kwargs.keys():
        if kwargs['rand_aug'] is True:
            n = kwargs['randaug_n']
            m = kwargs['randaug_m']
            train_transform.transforms.insert(2, RandAugment(m=m, n=n))

    # if 'temporal_flatten' in kwargs.keys():
    #     if kwargs['temporal_flatten'] is True:
    #         train_transform.transforms.insert(-1, lambda x: temporal_flatten(x))
    #         test_transform.transforms.insert(-1, lambda x: temporal_flatten(x))

    train_dataset = DiskCachedDataset(train_dataset,
                                      cache_path=os.path.join(root, 'DVS/NCALTECH101/train_cache_{}'.format(step)),
                                      transform=train_transform, num_copies=3)
    test_dataset = DiskCachedDataset(test_dataset,
                                     cache_path=os.path.join(root, 'DVS/NCALTECH101/test_cache_{}'.format(step)),
                                     transform=test_transform, num_copies=3)

    mix_up, cut_mix, event_mix, beta, prob, num, num_classes, noise, gaussian_n = unpack_mix_param(kwargs)
    mixup_active = cut_mix | event_mix | mix_up

    if cut_mix:
        train_dataset = CutMix(train_dataset,
                               beta=beta,
                               prob=prob,
                               num_mix=num,
                               num_class=num_classes,
                               indices=train_sample_index,
                               noise=noise)

    if event_mix:
        train_dataset = EventMix(train_dataset,
                                 beta=beta,
                                 prob=prob,
                                 num_mix=num,
                                 num_class=num_classes,
                                 indices=train_sample_index,
                                 noise=noise,
                                 gaussian_n=gaussian_n)
    if mix_up:
        train_dataset = MixUp(train_dataset,
                              beta=beta,
                              prob=prob,
                              num_mix=num,
                              num_class=num_classes,
                              indices=train_sample_index,
                              noise=noise)

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=batch_size,
        sampler=train_sampler,
        pin_memory=True, drop_last=True, num_workers=8
    )

    test_loader = torch.utils.data.DataLoader(
        test_dataset, batch_size=batch_size,
        sampler=test_sampler,
        pin_memory=True, drop_last=False, num_workers=2
    )

    return train_loader, test_loader, mixup_active, None


def get_NCARS_data(batch_size, step, root=DATA_DIR, **kwargs):
    """
    获取N-Cars数据
    https://ieeexplore.ieee.org/document/8578284/
    :param batch_size: batch size
    :param step: 仿真步长
    :param kwargs:
    :return: (train loader, test loader, mixup_active, mixup_fn)
    """
    sensor_size = tonic.datasets.NCARS.sensor_size
    size = kwargs['size'] if 'size' in kwargs else 48

    train_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        # tonic.transforms.DropEvent(p=0.1),
        tonic.transforms.ToFrame(sensor_size=None, n_time_bins=step),
    ])
    test_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        tonic.transforms.ToFrame(sensor_size=None, n_time_bins=step),
    ])

    train_dataset = tonic.datasets.NCARS(os.path.join(root, 'DVS/NCARS'), transform=train_transform, train=True)
    test_dataset = tonic.datasets.NCARS(os.path.join(root, 'DVS/NCARS'), transform=test_transform, train=False)

    train_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        lambda x: F.interpolate(x, size=[size, size], mode='bilinear', align_corners=True),
        lambda x: dvs_channel_check_expend(x),
        transforms.RandomCrop(size, padding=size // 12),
        transforms.RandomHorizontalFlip(),
        transforms.RandomRotation(15)
    ])
    test_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        lambda x: F.interpolate(x, size=[size, size], mode='bilinear', align_corners=True),
        lambda x: dvs_channel_check_expend(x),
    ])
    if 'rand_aug' in kwargs.keys():
        if kwargs['rand_aug'] is True:
            n = kwargs['randaug_n']
            m = kwargs['randaug_m']
            train_transform.transforms.insert(2, RandAugment(m=m, n=n))

    # if 'temporal_flatten' in kwargs.keys():
    #     if kwargs['temporal_flatten'] is True:
    #         train_transform.transforms.insert(-1, lambda x: temporal_flatten(x))
    #         test_transform.transforms.insert(-1, lambda x: temporal_flatten(x))

    train_dataset = DiskCachedDataset(train_dataset,
                                      cache_path=os.path.join(root, 'DVS/NCARS/train_cache_{}'.format(step)),
                                      transform=train_transform, num_copies=3)
    test_dataset = DiskCachedDataset(test_dataset,
                                     cache_path=os.path.join(root, 'DVS/NCARS/test_cache_{}'.format(step)),
                                     transform=test_transform, num_copies=3)

    mix_up, cut_mix, event_mix, beta, prob, num, num_classes, noise, gaussian_n = unpack_mix_param(kwargs)
    mixup_active = cut_mix | event_mix | mix_up

    if cut_mix:
        train_dataset = CutMix(train_dataset,
                               beta=beta,
                               prob=prob,
                               num_mix=num,
                               num_class=num_classes,
                               noise=noise)

    if event_mix:
        train_dataset = EventMix(train_dataset,
                                 beta=beta,
                                 prob=prob,
                                 num_mix=num,
                                 num_class=num_classes,
                                 noise=noise,
                                 gaussian_n=gaussian_n)
    if mix_up:
        train_dataset = MixUp(train_dataset,
                              beta=beta,
                              prob=prob,
                              num_mix=num,
                              num_class=num_classes,
                              noise=noise)

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=batch_size,
        pin_memory=True, drop_last=True, num_workers=8,
        shuffle=True,
    )

    test_loader = torch.utils.data.DataLoader(
        test_dataset, batch_size=batch_size,
        pin_memory=True, drop_last=False, num_workers=2,
        shuffle=False,
    )

    return train_loader, test_loader, mixup_active, None


def get_nomni_data(batch_size, train_portion=1., root=DATA_DIR, **kwargs):
    """
    获取N-Omniglot数据
    :param batch_size:batch的大小
    :param data_mode:一共full nkks pair三种模式
    :param frames_num:一个样本帧的个数
    :param data_type:event frequency两种模式
    """
    data_mode = kwargs["data_mode"] if "data_mode" in kwargs else "full"
    frames_num = kwargs["frames_num"] if "frames_num" in kwargs else 4
    data_type = kwargs["data_type"] if "data_type" in kwargs else "event"

    train_transform = transforms.Compose([
        transforms.Resize((28, 28))])
    test_transform = transforms.Compose([
        transforms.Resize((28, 28))])
    if data_mode == "full":
        train_datasets = NOmniglotfull(root=os.path.join(root, 'DVS/NOmniglot'), train=True, frames_num=frames_num,
                                       data_type=data_type,
                                       transform=train_transform)
        test_datasets = NOmniglotfull(root=os.path.join(root, 'DVS/NOmniglot'), train=False, frames_num=frames_num,
                                      data_type=data_type,
                                      transform=test_transform)

    elif data_mode == "nkks":
        train_datasets = NOmniglotNWayKShot(os.path.join(root, 'DVS/NOmniglot'),
                                            n_way=kwargs["n_way"],
                                            k_shot=kwargs["k_shot"],
                                            k_query=kwargs["k_query"],
                                            train=True,
                                            frames_num=frames_num,
                                            data_type=data_type,
                                            transform=train_transform)
        test_datasets = NOmniglotNWayKShot(os.path.join(root, 'DVS/NOmniglot'),
                                           n_way=kwargs["n_way"],
                                           k_shot=kwargs["k_shot"],
                                           k_query=kwargs["k_query"],
                                           train=False,
                                           frames_num=frames_num,
                                           data_type=data_type,
                                           transform=test_transform)
    elif data_mode == "pair":
        train_datasets = NOmniglotTrainSet(root=os.path.join(root, 'DVS/NOmniglot'), use_frame=True,
                                           frames_num=frames_num, data_type=data_type,
                                           use_npz=False, resize=105)
        test_datasets = NOmniglotTestSet(root=os.path.join(root, 'DVS/NOmniglot'), time=2000, way=kwargs["n_way"],
                                         shot=kwargs["k_shot"], use_frame=True,
                                         frames_num=frames_num, data_type=data_type, use_npz=False, resize=105)

    else:
        pass

    train_loader = torch.utils.data.DataLoader(
        train_datasets, batch_size=batch_size, num_workers=12,
        pin_memory=True, drop_last=True, shuffle=True
    )
    test_loader = torch.utils.data.DataLoader(
        test_datasets, batch_size=batch_size, num_workers=12,
        pin_memory=True, drop_last=False
    )
    return train_loader, test_loader, None, None


def get_esimnet_data(batch_size, step, root=DATA_DIR, **kwargs):
    """
    获取ES imagenet数据
    DOI: 10.3389/fnins.2021.726582
    :param batch_size: batch size
    :param step: 仿真步长，固定为8
    :param reconstruct: 重构则时间步为1, 否则为8
    :param kwargs:
    :return: (train loader, test loader, mixup_active, mixup_fn)
    :note: 没有自动下载, 下载及md5请参考spikingjelly, sampler默认为DistributedSampler
    """

    reconstruct = kwargs["reconstruct"] if "reconstruct" in kwargs else False

    train_transform = transforms.Compose([
        transforms.RandomHorizontalFlip(),
        transforms.RandomRotation(15)
    ])
    test_transform = transforms.Compose([
        lambda x: dvs_channel_check_expend(x),
    ])

    if reconstruct:
        assert step == 1
        train_dataset = ESImagenet2D_Dataset(mode='train',
                                             data_set_path=os.path.join(root,
                                                                        'DVS/ES-imagenet-0.18/extract/ES-imagenet-0.18/'),
                                             transform=train_transform)

        test_dataset = ESImagenet2D_Dataset(mode='test',
                                            data_set_path=os.path.join(root,
                                                                       'DVS/ES-imagenet-0.18/extract/ES-imagenet-0.18/'),
                                            transform=test_transform)
    else:
        assert step == 8
        train_dataset = ESImagenet_Dataset(mode='train',
                                           data_set_path=os.path.join(root,
                                                                      'DVS/ES-imagenet-0.18/extract/ES-imagenet-0.18/'),
                                           transform=train_transform)

        test_dataset = ESImagenet_Dataset(mode='test',
                                          data_set_path=os.path.join(root,
                                                                     'DVS/ES-imagenet-0.18/extract/ES-imagenet-0.18/'),
                                          transform=test_transform)

    mix_up, cut_mix, event_mix, beta, prob, num, num_classes, noise, gaussian_n = unpack_mix_param(kwargs)
    mixup_active = cut_mix | event_mix | mix_up

    if cut_mix:
        train_dataset = CutMix(train_dataset,
                               beta=beta,
                               prob=prob,
                               num_mix=num,
                               num_class=num_classes,
                               noise=noise)

    if event_mix:
        train_dataset = EventMix(train_dataset,
                                 beta=beta,
                                 prob=prob,
                                 num_mix=num,
                                 num_class=num_classes,
                                 noise=noise,
                                 gaussian_n=gaussian_n)
    if mix_up:
        train_dataset = MixUp(train_dataset,
                              beta=beta,
                              prob=prob,
                              num_mix=num,
                              num_class=num_classes,
                              noise=noise)

    train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
    test_sampler = torch.utils.data.distributed.DistributedSampler(test_dataset)
    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=batch_size,
        pin_memory=True, drop_last=True, num_workers=8,
        shuffle=False, sampler=train_sampler
    )

    test_loader = torch.utils.data.DataLoader(
        test_dataset, batch_size=batch_size,
        pin_memory=True, drop_last=False, num_workers=1,
        shuffle=False, sampler=test_sampler
    )

    return train_loader, test_loader, mixup_active, None


def get_nmnist_data(batch_size, step, **kwargs):
    """
    获取N-MNIST数据
    http://journal.frontiersin.org/Article/10.3389/fnins.2015.00437/abstract
    :param batch_size: batch size
    :param step: 仿真步长
    :param kwargs:
    :return: (train loader, test loader, mixup_active, mixup_fn)
    """
    sensor_size = tonic.datasets.NMNIST.sensor_size
    size = kwargs['size'] if 'size' in kwargs else 34

    train_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        # tonic.transforms.DropEvent(p=0.1),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step),
    ])
    test_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step),
    ])

    train_dataset = tonic.datasets.NMNIST(os.path.join(DATA_DIR, 'DVS/N-MNIST'),
                                          transform=train_transform, train=True)
    test_dataset = tonic.datasets.NMNIST(os.path.join(DATA_DIR, 'DVS/N-MNIST'),
                                         transform=test_transform, train=False)

    train_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        lambda x: F.interpolate(x, size=[size, size], mode='bilinear', align_corners=True),
        lambda x: dvs_channel_check_expend(x),
        # transforms.RandomCrop(size, padding=size // 12),
        # transforms.RandomHorizontalFlip(),
        # transforms.RandomRotation(15)
    ])
    test_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        lambda x: F.interpolate(x, size=[size, size], mode='bilinear', align_corners=True),
        lambda x: dvs_channel_check_expend(x),
    ])
    if 'rand_aug' in kwargs.keys():
        if kwargs['rand_aug'] is True:
            n = kwargs['randaug_n']
            m = kwargs['randaug_m']
            train_transform.transforms.insert(2, RandAugment(m=m, n=n))

    # if 'temporal_flatten' in kwargs.keys():
    #     if kwargs['temporal_flatten'] is True:
    #         train_transform.transforms.insert(-1, lambda x: temporal_flatten(x))
    #         test_transform.transforms.insert(-1, lambda x: temporal_flatten(x))

    train_dataset = DiskCachedDataset(train_dataset,
                                      cache_path=os.path.join(DATA_DIR, 'DVS/N-MNIST/train_cache_{}'.format(step)),
                                      transform=train_transform, num_copies=3)
    test_dataset = DiskCachedDataset(test_dataset,
                                     cache_path=os.path.join(DATA_DIR, 'DVS/N-MNIST/test_cache_{}'.format(step)),
                                     transform=test_transform, num_copies=3)

    mix_up, cut_mix, event_mix, beta, prob, num, num_classes, noise, gaussian_n = unpack_mix_param(kwargs)
    mixup_active = cut_mix | event_mix | mix_up

    if cut_mix:
        train_dataset = CutMix(train_dataset,
                               beta=beta,
                               prob=prob,
                               num_mix=num,
                               num_class=num_classes,
                               noise=noise)

    if event_mix:
        train_dataset = EventMix(train_dataset,
                                 beta=beta,
                                 prob=prob,
                                 num_mix=num,
                                 num_class=num_classes,
                                 noise=noise,
                                 gaussian_n=gaussian_n)
    if mix_up:
        train_dataset = MixUp(train_dataset,
                              beta=beta,
                              prob=prob,
                              num_mix=num,
                              num_class=num_classes,
                              noise=noise)

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=batch_size,
        pin_memory=True, drop_last=True, num_workers=8,
        shuffle=True,
    )

    test_loader = torch.utils.data.DataLoader(
        test_dataset, batch_size=batch_size,
        pin_memory=True, drop_last=False, num_workers=2,
        shuffle=False,
    )

    return train_loader, test_loader, mixup_active, None


def get_shd_data(batch_size, step, **kwargs):
    """
    获取SHD数据
    https://ieeexplore.ieee.org/abstract/document/9311226
    :param batch_size: batch size
    :param step: 仿真步长
    :param kwargs:
    :return: (train loader, test loader, mixup_active, mixup_fn)
    :format: (b,t,c,len) 不同于vision, audio中c为1, 并且没有h,w; 只有len=700. Transform后变为(b, t, len)
    """
    sensor_size = tonic.datasets.SHD.sensor_size
    train_transform = transforms.Compose([
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step)
    ])
    test_transform = transforms.Compose([
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step)
    ])

    train_dataset = tonic.datasets.SHD(os.path.join(DATA_DIR, 'DVS/SHD'),
                                       transform=train_transform, train=True)

    test_dataset = tonic.datasets.SHD(os.path.join(DATA_DIR, 'DVS/SHD'),
                                      transform=test_transform, train=False)

    train_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        lambda x: x.squeeze(1)
    ])

    test_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        lambda x: x.squeeze(1)
    ])

    train_dataset = DiskCachedDataset(train_dataset,
                                      cache_path=os.path.join(DATA_DIR, 'DVS/SHD/train_cache_{}'.format(step)),
                                      transform=train_transform, num_copies=3)
    test_dataset = DiskCachedDataset(test_dataset,
                                     cache_path=os.path.join(DATA_DIR, 'DVS/SHD/test_cache_{}'.format(step)),
                                     transform=test_transform, num_copies=3)

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=batch_size,
        pin_memory=True, drop_last=False, num_workers=8,
        shuffle=True,
    )

    test_loader = torch.utils.data.DataLoader(
        test_dataset, batch_size=batch_size,
        pin_memory=True, drop_last=False, num_workers=2,
        shuffle=False,
    )

    return train_loader, test_loader, None, None


def get_CUB2002011_data(batch_size, num_workers=8, same_da=False, root=DATA_DIR, *args, **kwargs):
    train_transform = transforms.Compose([
        transforms.RandomResizedCrop(224),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))
    ])
    test_transform = transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))
    ])
    root = os.path.join(root, 'CUB2002011')
    train_datasets = CUB2002011(
        root=root, train=True, transform=test_transform if same_da else train_transform, download=True)
    test_datasets = CUB2002011(
        root=root, train=False, transform=test_transform, download=True)

    train_loader = torch.utils.data.DataLoader(
        train_datasets, batch_size=batch_size,
        pin_memory=True, drop_last=True, shuffle=True, num_workers=num_workers
    )

    test_loader = torch.utils.data.DataLoader(
        test_datasets, batch_size=batch_size,
        pin_memory=True, drop_last=False, num_workers=num_workers
    )

    return train_loader, test_loader, False, None


def get_StanfordCars_data(batch_size, num_workers=8, same_da=False, root=DATA_DIR, *args, **kwargs):
    train_transform = transforms.Compose([
        transforms.RandomResizedCrop(224),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))
    ])
    test_transform = transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))
    ])
    root = os.path.join(root, 'StanfordCars')
    train_datasets = datasets.StanfordCars(
        root=root, split="train", transform=test_transform if same_da else train_transform, download=True)
    test_datasets = datasets.StanfordCars(
        root=root, split="test", transform=test_transform, download=True)

    train_loader = torch.utils.data.DataLoader(
        train_datasets, batch_size=batch_size,
        pin_memory=True, drop_last=True, shuffle=True, num_workers=num_workers
    )

    test_loader = torch.utils.data.DataLoader(
        test_datasets, batch_size=batch_size,
        pin_memory=True, drop_last=False, num_workers=num_workers
    )

    return train_loader, test_loader, False, None


def get_StanfordDogs_data(batch_size, num_workers=8, same_da=False, root=DATA_DIR, *args, **kwargs):
    train_transform = transforms.Compose([
        transforms.RandomResizedCrop(224),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))
    ])
    test_transform = transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))
    ])
    root = os.path.join(root, 'StanfordDogs')
    train_datasets = StanfordDogs(
        root=root, train=True, transform=test_transform if same_da else train_transform, download=True)
    test_datasets = StanfordDogs(
        root=root, train=False, transform=test_transform, download=True)

    train_loader = torch.utils.data.DataLoader(
        train_datasets, batch_size=batch_size,
        pin_memory=True, drop_last=True, shuffle=True, num_workers=num_workers
    )

    test_loader = torch.utils.data.DataLoader(
        test_datasets, batch_size=batch_size,
        pin_memory=True, drop_last=False, num_workers=num_workers
    )

    return train_loader, test_loader, False, None


def get_FGVCAircraft_data(batch_size, num_workers=8, same_da=False, root=DATA_DIR, *args, **kwargs):
    train_transform = transforms.Compose([
        transforms.RandomResizedCrop(224),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))
    ])
    test_transform = transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))
    ])
    root = os.path.join(root, 'FGVCAircraft')
    train_datasets = datasets.FGVCAircraft(
        root=root, split="train", transform=test_transform if same_da else train_transform, download=True)
    test_datasets = datasets.FGVCAircraft(
        root=root, split="test", transform=test_transform, download=True)

    train_loader = torch.utils.data.DataLoader(
        train_datasets, batch_size=batch_size,
        pin_memory=True, drop_last=True, shuffle=True, num_workers=num_workers
    )

    test_loader = torch.utils.data.DataLoader(
        test_datasets, batch_size=batch_size,
        pin_memory=True, drop_last=False, num_workers=num_workers
    )

    return train_loader, test_loader, False, None


def get_Flowers102_data(batch_size, num_workers=8, same_da=False, root=DATA_DIR, *args, **kwargs):
    train_transform = transforms.Compose([
        transforms.RandomResizedCrop(224),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))
    ])
    test_transform = transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))
    ])
    root = os.path.join(root, 'Flowers102')
    train_datasets = datasets.Flowers102(
        root=root, split="train", transform=test_transform if same_da else train_transform, download=True)
    test_datasets = datasets.Flowers102(
        root=root, split="test", transform=test_transform, download=True)

    train_loader = torch.utils.data.DataLoader(
        train_datasets, batch_size=batch_size,
        pin_memory=True, drop_last=True, shuffle=True, num_workers=num_workers
    )

    test_loader = torch.utils.data.DataLoader(
        test_datasets, batch_size=batch_size,
        pin_memory=True, drop_last=False, num_workers=num_workers
    )

    return train_loader, test_loader, False, None


def get_UCF101DVS_data(batch_size, step, **kwargs):
    """
    获取DVS CIFAR10数据
    http://journal.frontiersin.org/article/10.3389/fnins.2017.00309/full
    :param batch_size: batch size
    :param step: 仿真步长
    :param kwargs:
    :return: (train loader, test loader, mixup_active, mixup_fn)
    """
    size = kwargs['size'] if 'size' in kwargs else 48
    sensor_size = braincog.datasets.ucf101_dvs.UCF101DVS.sensor_size
    train_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        # tonic.transforms.DropEvent(p=0.1),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step), ])
    test_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step), ])
    train_dataset = braincog.datasets.ucf101_dvs.UCF101DVS(os.path.join(DATA_DIR, 'UCF101DVS'), train=True,
                                                           transform=train_transform)
    test_dataset = braincog.datasets.ucf101_dvs.UCF101DVS(os.path.join(DATA_DIR, 'UCF101DVS'), train=False,
                                                          transform=test_transform)

    train_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        # lambda x: F.interpolate(x, size=[size, size], mode='bilinear', align_corners=True),
        # lambda x: TemporalShift(x, .01),
        # lambda x: drop(x, 0.15),
        # lambda x: ShearX(x, 15),
        # lambda x: ShearY(x, 15),
        # lambda x: TranslateX(x, 0.225),
        # lambda x: TranslateY(x, 0.225),
        # lambda x: Rotate(x, 15),
        # lambda x: CutoutAbs(x, 0.25),
        # lambda x: CutoutTemporal(x, 0.25),
        # lambda x: GaussianBlur(x, 0.5),
        # lambda x: SaltAndPepperNoise(x, 0.1),
        # transforms.Normalize(DVSCIFAR10_MEAN_16, DVSCIFAR10_STD_16),
        # transforms.RandomCrop(size, padding=size // 12),
        transforms.RandomHorizontalFlip(),
        # transforms.RandomRotation(15)
    ])
    test_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        # lambda x: F.interpolate(x, size=[size, size], mode='bilinear', align_corners=True),
    ])

    if 'rand_aug' in kwargs.keys():
        if kwargs['rand_aug'] is True:
            n = kwargs['randaug_n']
            m = kwargs['randaug_m']
            # print('randaug', m, n)
            train_transform.transforms.insert(2, RandAugment(m=m, n=n))

    # if 'temporal_flatten' in kwargs.keys():
    #     if kwargs['temporal_flatten'] is True:
    #         train_transform.transforms.insert(-1, lambda x: temporal_flatten(x))
    #         test_transform.transforms.insert(-1, lambda x: temporal_flatten(x))

    train_dataset = DiskCachedDataset(train_dataset,
                                      cache_path=os.path.join(DATA_DIR, 'UCF101DVS/train_cache_{}'.format(step)),
                                      transform=train_transform)
    test_dataset = DiskCachedDataset(test_dataset,
                                     cache_path=os.path.join(DATA_DIR, 'UCF101DVS/test_cache_{}'.format(step)),
                                     transform=test_transform)

    mix_up, cut_mix, event_mix, beta, prob, num, num_classes, noise, gaussian_n = unpack_mix_param(kwargs)
    mixup_active = cut_mix | event_mix | mix_up

    if cut_mix:
        # print('cut_mix', beta, prob, num, num_classes)
        train_dataset = CutMix(train_dataset,
                               beta=beta,
                               prob=prob,
                               num_mix=num,
                               num_class=num_classes,
                               noise=noise)

    if event_mix:
        train_dataset = EventMix(train_dataset,
                                 beta=beta,
                                 prob=prob,
                                 num_mix=num,
                                 num_class=num_classes,
                                 noise=noise,
                                 gaussian_n=gaussian_n)

    if mix_up:
        train_dataset = MixUp(train_dataset,
                              beta=beta,
                              prob=prob,
                              num_mix=num,
                              num_class=num_classes,
                              noise=noise)

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=batch_size, shuffle=True,
        pin_memory=True, drop_last=True, num_workers=8
    )

    test_loader = torch.utils.data.DataLoader(
        test_dataset, batch_size=batch_size, shuffle=False,
        pin_memory=True, drop_last=False, num_workers=2
    )

    return train_loader, test_loader, mixup_active, None


def get_HMDBDVS_data(batch_size, step, **kwargs):
    sensor_size = braincog.datasets.hmdb_dvs.HMDBDVS.sensor_size

    train_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        # tonic.transforms.DropEvent(p=0.1),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step), ])
    test_transform = transforms.Compose([
        # tonic.transforms.Denoise(filter_time=10000),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step), ])

    train_dataset = braincog.datasets.hmdb_dvs.HMDBDVS(os.path.join(DATA_DIR, 'HMDBDVS'), transform=train_transform)
    test_dataset = braincog.datasets.hmdb_dvs.HMDBDVS(os.path.join(DATA_DIR, 'HMDBDVS'), transform=test_transform)

    cls_count = train_dataset.cls_count
    dataset_length = train_dataset.length

    portion = .5
    # portion = kwargs['portion'] if 'portion' in kwargs else .9
    size = kwargs['size'] if 'size' in kwargs else 48
    # print('portion', portion)
    train_sample_weight = []
    train_sample_index = []
    train_count = 0
    test_sample_index = []
    idx_begin = 0
    for count in cls_count:
        sample_weight = dataset_length / count
        train_sample = round(portion * count)
        test_sample = count - train_sample
        train_count += train_sample
        train_sample_weight.extend(
            [sample_weight] * train_sample
        )
        train_sample_weight.extend(
            [0.] * test_sample
        )
        lst = list(range(idx_begin, idx_begin + train_sample + test_sample))
        random.seed(0)
        random.shuffle(lst)
        train_sample_index.extend(
            lst[:train_sample]
            # list((range(idx_begin, idx_begin + train_sample)))
        )
        test_sample_index.extend(
            lst[train_sample:train_sample + test_sample]
            # list(range(idx_begin + train_sample, idx_begin + train_sample + test_sample))
        )
        idx_begin += count

    train_sampler = torch.utils.data.sampler.WeightedRandomSampler(train_sample_weight, train_count)
    test_sampler = torch.utils.data.sampler.SubsetRandomSampler(test_sample_index)

    train_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        # lambda x: print(x.shape),
        # lambda x: F.interpolate(x, size=[size, size], mode='bilinear', align_corners=True),
        # transforms.RandomCrop(size, padding=size // 12),
        # transforms.RandomHorizontalFlip(),
        # transforms.RandomRotation(15)
    ])
    test_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        # lambda x: F.interpolate(x, size=[size, size], mode='bilinear', align_corners=True),
        # lambda x: temporal_flatten(x),
    ])
    if 'rand_aug' in kwargs.keys():
        if kwargs['rand_aug'] is True:
            n = kwargs['randaug_n']
            m = kwargs['randaug_m']
            train_transform.transforms.insert(2, RandAugment(m=m, n=n))

    # if 'temporal_flatten' in kwargs.keys():
    #     if kwargs['temporal_flatten'] is True:
    #         train_transform.transforms.insert(-1, lambda x: temporal_flatten(x))
    #         test_transform.transforms.insert(-1, lambda x: temporal_flatten(x))

    train_dataset = DiskCachedDataset(train_dataset,
                                      cache_path=os.path.join(DATA_DIR, 'HMDBDVS/train_cache_{}'.format(step)),
                                      transform=train_transform, num_copies=3)
    test_dataset = DiskCachedDataset(test_dataset,
                                     cache_path=os.path.join(DATA_DIR, 'HMDBDVS/test_cache_{}'.format(step)),
                                     transform=test_transform, num_copies=3)

    mix_up, cut_mix, event_mix, beta, prob, num, num_classes, noise, gaussian_n = unpack_mix_param(kwargs)
    mixup_active = cut_mix | event_mix | mix_up

    if cut_mix:
        train_dataset = CutMix(train_dataset,
                               beta=beta,
                               prob=prob,
                               num_mix=num,
                               num_class=num_classes,
                               indices=train_sample_index,
                               noise=noise)

    if event_mix:
        train_dataset = EventMix(train_dataset,
                                 beta=beta,
                                 prob=prob,
                                 num_mix=num,
                                 num_class=num_classes,
                                 indices=train_sample_index,
                                 noise=noise,
                                 gaussian_n=gaussian_n)
    if mix_up:
        train_dataset = MixUp(train_dataset,
                              beta=beta,
                              prob=prob,
                              num_mix=num,
                              num_class=num_classes,
                              indices=train_sample_index,
                              noise=noise)

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=batch_size,
        sampler=train_sampler,
        pin_memory=True, drop_last=True, num_workers=8
    )

    test_loader = torch.utils.data.DataLoader(
        test_dataset, batch_size=batch_size,
        sampler=test_sampler,
        pin_memory=True, drop_last=False, num_workers=2
    )

    return train_loader, test_loader, mixup_active, None
