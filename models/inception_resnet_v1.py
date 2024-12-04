import os
from typing import Dict, List, Optional

import torch
from nnt import log
from torch import nn
from torch.nn import functional as F

from .utils.download import download_url_to_file


class CustomNNModule(nn.Module):
    """Wrapper for nn.Module with additional method to calculate params.

    Standard nn.Module was not properly calculating the number of parameters
    for Convolutional layers with modified kernel size. This class is a workaround.
    """

    def get_parameters(self):
        return get_parameters(self)


def calculate_superconv2d_params(superconv_layer):
    """
    Calculate the number of parameters used by the SuperConv2D layer
    based on its runtime configuration.

    Args:
        superconv_layer (SuperConv2D): The custom SuperConv2D layer.

    Returns:
        int: Number of parameters used by the layer.
        float: Size of the parameters in MB.
    """
    # Extract runtime configuration
    subnet_in_dim = superconv_layer.subnet_in_dim
    subnet_out_dim = superconv_layer.subnet_out_dim
    subnet_kernel_size = superconv_layer.subnet_kernel_size

    # Calculate number of parameters in the weight matrix
    weight_params = subnet_in_dim * subnet_out_dim * (subnet_kernel_size**2)
    weight_size = weight_params * superconv_layer.weight.element_size()  # In bytes

    # Calculate bias parameters (if bias is not None)
    bias_params = 0
    if superconv_layer.bias is not None:
        bias_params = subnet_out_dim
        weight_size += (
            bias_params * superconv_layer.bias.element_size()
        )  # Add bias size

    total_params = weight_params + bias_params
    return total_params, weight_size / (1024**2)  # Convert to MB


def get_parameters(model: nn.Module) -> int:
    parameters = []
    for name, module in model.named_children():
        if hasattr(module, "get_parameters"):
            module_params = module.get_parameters()
            # log.warning(f"GET_PARTAMS {name}: {module_params}")

        elif isinstance(module, BasicConv2d) and isinstance(module.conv, SuperConv2D):
            module_params = calculate_superconv2d_params(module.conv)[0]
            module_params += sum(p.numel() for p in module.bn.parameters())
            module_params += sum(p.numel() for p in module.relu.parameters())
            # log.warning(f"{name}: {module_params}")
        elif isinstance(module, SuperConv2D):
            module_params = calculate_superconv2d_params(module)[0]
            # log.warning(f"{name}: {module_params}")
        elif isinstance(module, nn.Sequential):
            module_params = 0
            # log.warning(f"Sequential: {name}: {get_parameters(module)}")
            for inseq_name, inseq_module in module.named_children():
                module_params += get_parameters(inseq_module)
                # log.warning(f"Sequential: {inseq_name} {type(inseq_module)}: {module_params}")
        else:
            module_params = sum(p.numel() for p in module.parameters())
            # log.error(f"{name}, {type(module)}: {module_params}")
        parameters.append(module_params)
    return sum(parameters)


class BasicConv2d(nn.Module):

    def __init__(
        self,
        in_planes,
        out_planes,
        kernel_size,
        stride,
        padding=0,
        Conv2d_class=nn.Conv2d,
    ):
        # NOTE: padding "same" can be used only when stride==1 https://github.com/pytorch/pytorch/issues/67551
        super().__init__()
        self.conv = Conv2d_class(
            in_planes,
            out_planes,
            kernel_size,
            stride=stride,
            padding=padding,
            bias=False,
        )  # verify bias false
        self.bn = nn.BatchNorm2d(
            out_planes,
            eps=0.001,  # value found in tensorflow
            momentum=0.1,  # default pytorch value
            affine=True,
        )
        self.relu = nn.ReLU(inplace=False)

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        x = self.relu(x)
        return x


class SuperConv2D(nn.Conv2d):

    def __init__(
        self,
        super_in_dim,
        super_out_dim,
        super_kernel_size,
        stride=(1, 1),
        padding=(1, 1),
        bias=False,
    ):
        super().__init__(
            super_in_dim,
            super_out_dim,
            super_kernel_size,
            stride=stride,
            padding=padding,
            bias=bias,
        )

        #         # Define SuperConv2D Layer Maximum Bounds
        self.super_in_dim = super_in_dim  # input channels
        self.super_out_dim = super_out_dim  # output channels
        self.super_kernel_size = super_kernel_size  # Kernel

        self.stride = stride
        self.padding = padding

        self.subnet = {}
        super().reset_parameters()
        self.profiling = False

        self.set_subnet_config(
            self.super_in_dim, self.super_out_dim, self.super_kernel_size
        )

    def set_subnet_config(
        self,
        subnet_in_dim: Optional[int] = None,
        subnet_out_dim: Optional[int] = None,
        subnet_kernel_size: Optional[int] = None,
    ):
        if subnet_in_dim is not None:
            self.subnet_in_dim = subnet_in_dim

        if subnet_out_dim is not None:
            self.subnet_out_dim = subnet_out_dim

        if subnet_kernel_size is not None:
            self.subnet_kernel_size = subnet_kernel_size
        self._subnet_parameters()

    def _subnet_parameters(self):
        self.subnet["weight"] = self._subselect_weight(
            self.weight,
            self.subnet_in_dim,
            self.subnet_out_dim,
            self.subnet_kernel_size,
        ).to("cuda")

        if self.bias is not None:
            self.subnet["bias"] = self._subselect_bias(self.bias, self.subnet_out_dim)
            self.subnet["bias"] = self.subnet["bias"].to("cuda")

    def forward(self, x):
        self._subnet_parameters()
        # input, weight, bias=None, stride=1, padding=0, dilation=1, groups=1
        conv_out = F.conv2d(
            input=x,
            weight=self.subnet["weight"],
            # bias=self.bias,
            stride=self.stride,
            padding=self.padding,
        )
        return conv_out

    @staticmethod
    def _subselect_weight(weight, subnet_in_dim, subnet_out_dim, subnet_kernel_size):
        # Weight matrix for Conv2d = [out_channels, in_channels, kernel, kernel]
        subnet_weight = weight[
            :subnet_out_dim, :subnet_in_dim, :subnet_kernel_size, :subnet_kernel_size
        ]

        return subnet_weight

    @staticmethod
    def _subselect_bias(bias, subnet_out_dim):
        subnet_bias = bias[:subnet_out_dim]
        return subnet_bias


class Block35(CustomNNModule):

    def __init__(self, scale=1.0):
        super().__init__()

        self.scale = scale

        self.branch0 = BasicConv2d(256, 32, kernel_size=1, stride=1)

        self.branch1 = nn.Sequential(
            BasicConv2d(256, 32, kernel_size=1, stride=1),
            BasicConv2d(
                32,
                32,
                kernel_size=3,
                stride=1,
                padding="same",
                Conv2d_class=SuperConv2D,
            ),
        )

        self.branch2 = nn.Sequential(
            BasicConv2d(256, 32, kernel_size=1, stride=1),
            BasicConv2d(
                32,
                32,
                kernel_size=3,
                stride=1,
                padding="same",
                Conv2d_class=SuperConv2D,
            ),
            BasicConv2d(
                32,
                32,
                kernel_size=3,
                stride=1,
                padding="same",
                Conv2d_class=SuperConv2D,
            ),
        )

        self.conv2d = nn.Conv2d(96, 256, kernel_size=1, stride=1)
        self.relu = nn.ReLU(inplace=False)

    def forward(self, x):
        x0 = self.branch0(x)
        x1 = self.branch1(x)
        x2 = self.branch2(x)
        out = torch.cat((x0, x1, x2), 1)
        out = self.conv2d(out)
        out = out * self.scale + x
        out = self.relu(out)
        return out


class Block17(CustomNNModule):

    def __init__(self, scale=1.0):
        super().__init__()

        self.scale = scale

        self.branch0 = BasicConv2d(896, 128, kernel_size=1, stride=1)

        self.branch1 = nn.Sequential(
            BasicConv2d(896, 128, kernel_size=1, stride=1),
            BasicConv2d(128, 128, kernel_size=(1, 7), stride=1, padding=(0, 3)),
            BasicConv2d(128, 128, kernel_size=(7, 1), stride=1, padding=(3, 0)),
        )

        self.conv2d = nn.Conv2d(256, 896, kernel_size=1, stride=1)
        self.relu = nn.ReLU(inplace=False)

    def forward(self, x):
        x0 = self.branch0(x)
        x1 = self.branch1(x)
        out = torch.cat((x0, x1), 1)
        out = self.conv2d(out)
        out = out * self.scale + x
        out = self.relu(out)
        return out


class Block8(CustomNNModule):

    def __init__(self, scale=1.0, noReLU=False):
        super().__init__()

        self.scale = scale
        self.noReLU = noReLU

        self.branch0 = BasicConv2d(1792, 192, kernel_size=1, stride=1)

        self.branch1 = nn.Sequential(
            BasicConv2d(1792, 192, kernel_size=1, stride=1),
            BasicConv2d(192, 192, kernel_size=(1, 3), stride=1, padding=(0, 1)),
            BasicConv2d(192, 192, kernel_size=(3, 1), stride=1, padding=(1, 0)),
        )

        self.conv2d = nn.Conv2d(384, 1792, kernel_size=1, stride=1)
        if not self.noReLU:
            self.relu = nn.ReLU(inplace=False)

    def forward(self, x):
        x0 = self.branch0(x)
        x1 = self.branch1(x)
        out = torch.cat((x0, x1), 1)
        out = self.conv2d(out)
        out = out * self.scale + x
        if not self.noReLU:
            out = self.relu(out)
        return out


class Mixed_6a(CustomNNModule):

    def __init__(self):
        super().__init__()

        self.branch0 = BasicConv2d(256, 384, kernel_size=3, stride=2)

        self.branch1 = nn.Sequential(
            BasicConv2d(256, 192, kernel_size=1, stride=1),
            BasicConv2d(
                192,
                192,
                kernel_size=3,
                stride=1,
                padding="same",
                Conv2d_class=SuperConv2D,
            ),
            BasicConv2d(192, 256, kernel_size=3, stride=2),
        )

        self.branch2 = nn.MaxPool2d(3, stride=2)

    def forward(self, x):
        x0 = self.branch0(x)
        x1 = self.branch1(x)
        x2 = self.branch2(x)
        out = torch.cat((x0, x1, x2), 1)
        return out


class Mixed_7a(CustomNNModule):

    def __init__(self):
        super().__init__()

        self.branch0 = nn.Sequential(
            BasicConv2d(896, 256, kernel_size=1, stride=1),
            BasicConv2d(256, 384, kernel_size=3, stride=2),
        )

        self.branch1 = nn.Sequential(
            BasicConv2d(896, 256, kernel_size=1, stride=1),
            BasicConv2d(256, 256, kernel_size=3, stride=2),
        )

        self.branch2 = nn.Sequential(
            BasicConv2d(896, 256, kernel_size=1, stride=1),
            BasicConv2d(
                256,
                256,
                kernel_size=3,
                stride=1,
                padding="same",
                Conv2d_class=SuperConv2D,
            ),
            BasicConv2d(256, 256, kernel_size=3, stride=2),
        )

        self.branch3 = nn.MaxPool2d(3, stride=2)

    def forward(self, x):
        x0 = self.branch0(x)
        x1 = self.branch1(x)
        x2 = self.branch2(x)
        x3 = self.branch3(x)
        out = torch.cat((x0, x1, x2, x3), 1)
        return out


class SuperSequential(nn.Sequential):
    def __init__(self, *args):
        super().__init__(*args)
        self.num_layers = len(self)

    def forward(self, x, num_layers=None):
        if num_layers is None:
            num_layers = self.num_layers
        for i in range(num_layers):
            x = self[i](x)
        return x

    def set_num_layers(self, num_layers):
        self.num_layers = num_layers


class InceptionResnetV1(nn.Module):
    """Inception Resnet V1 model with optional loading of pretrained weights.

    Model parameters can be loaded based on pretraining on the VGGFace2 or CASIA-Webface
    datasets. Pretrained state_dicts are automatically downloaded on model instantiation if
    requested and cached in the torch cache. Subsequent instantiations use the cache rather than
    redownloading.

    Keyword Arguments:
        pretrained {str} -- Optional pretraining dataset. Either 'vggface2' or 'casia-webface'.
            (default: {None})
        classify {bool} -- Whether the model should output classification probabilities or feature
            embeddings. (default: {False})
        num_classes {int} -- Number of output classes. If 'pretrained' is set and num_classes not
            equal to that used for the pretrained model, the final linear layer will be randomly
            initialized. (default: {None})
        dropout_prob {float} -- Dropout probability. (default: {0.6})
    """

    def __init__(
        self,
        pretrained=None,
        classify=False,
        num_classes=None,
        dropout_prob=0.6,
        device=None,
    ):
        super().__init__()

        # Set simple attributes
        self.pretrained = pretrained
        self.classify = classify
        self.num_classes = num_classes
        tmp_classes = None  # just to calm down pylint

        if pretrained == "vggface2":
            tmp_classes = 8631
        elif pretrained == "casia-webface":
            tmp_classes = 10575
        elif pretrained:
            assert (
                num_classes is not None
            ), "num_classes must be specified when `pretrained` is set and different from 'vggface2' or 'casia-webface'"
            tmp_classes = num_classes
        elif pretrained is None and self.classify and self.num_classes is None:
            raise Exception(
                'If "pretrained" is not specified and "classify" is True, "num_classes" must be specified'
            )

        # Define layers
        self.conv2d_1a = BasicConv2d(3, 32, kernel_size=3, stride=2)
        self.conv2d_2a = BasicConv2d(
            32,
            32,
            kernel_size=3,
            stride=1,
            padding="same",
            Conv2d_class=SuperConv2D,
        )
        self.conv2d_2b = BasicConv2d(
            32,
            64,
            kernel_size=3,
            stride=1,
            padding="same",
            Conv2d_class=SuperConv2D,
        )
        self.maxpool_3a = nn.MaxPool2d(3, stride=2)
        self.conv2d_3b = BasicConv2d(64, 80, kernel_size=1, stride=1)
        self.conv2d_4a = BasicConv2d(80, 192, kernel_size=3, stride=1)
        self.conv2d_4b = BasicConv2d(192, 256, kernel_size=3, stride=2)
        self.repeat_1 = self._create_sequential(repeat=5, BlockType=Block35, scale=0.17)
        self.mixed_6a = Mixed_6a()
        self.repeat_2 = self._create_sequential(
            repeat=10, BlockType=Block17, scale=0.10
        )
        self.mixed_7a = Mixed_7a()
        self.repeat_3 = self._create_sequential(repeat=5, BlockType=Block8, scale=0.20)
        self.block8 = Block8(noReLU=True)
        self.avgpool_1a = nn.AdaptiveAvgPool2d(1)
        self.dropout = nn.Dropout(dropout_prob)
        self.last_linear = nn.Linear(1792, 512, bias=False)
        self.last_bn = nn.BatchNorm1d(512, eps=0.001, momentum=0.1, affine=True)

        if pretrained is not None:
            self.logits = nn.Linear(512, tmp_classes)
            log.info(
                f'Loading pretrained weights for "{pretrained}". Logits shape: {self.logits.weight.shape}'
            )
            load_weights(self, pretrained)

        if self.classify and self.num_classes is not None:
            self.logits = nn.Linear(512, self.num_classes)

        self.device = torch.device("cpu")
        if device is not None:
            self.device = device
            self.to(device)

        # self.set_config({"ks": [1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1]})
        self.set_config(
            {
                "ks": [3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3],
                "num_layers": [5, 10, 5],
            }
        )
        # self.conv2d_2b.conv.set_subnet_config(subnet_kernel_size=1)
        # self.conv2d_2a.conv.set_subnet_config(subnet_kernel_size=1)
        log.info(
            f"Current subnet config: [{len(self.get_config()['ks'])}] {self.get_config()}"
        )
        # exit()

    def _create_sequential(
        self, repeat: int = 5, BlockType: nn.Module = Block35, scale: float = 0.17
    ):
        return SuperSequential(*[BlockType(scale=scale) for _ in range(repeat)])

    def set_config(self, config: Dict[str, List[int]]):
        super_ops: List[SuperConv2D] = self.get_super_ops()
        for ks, op in zip(config["ks"], super_ops):
            op.set_subnet_config(subnet_kernel_size=ks)

        seq_ops = self.get_super_sequential_ops()
        for num_layers, op in zip(config["num_layers"], seq_ops):
            op.set_num_layers(num_layers)

    def get_config(self) -> Dict[str, List[int]]:
        super_ops = self.get_super_ops()
        super_sequential_ops = self.get_super_sequential_ops()
        config = {
            "ks": [op.subnet_kernel_size for op in super_ops],
            "num_layers": [op.num_layers for op in super_sequential_ops],
        }
        return config

    def get_super_ops(self) -> List[SuperConv2D]:
        super_ops: List[SuperConv2D] = [
            module
            for name, module in self.named_modules()
            if isinstance(module, SuperConv2D)
        ]
        return super_ops

    def get_super_sequential_ops(self) -> List[SuperSequential]:
        super_sequential_ops: List[SuperSequential] = [
            module for module in self.modules() if isinstance(module, SuperSequential)
        ]
        return super_sequential_ops

    def list_first_level_sequential_layers(self):
        """List first level sequential layers"""
        first_level_sequential_layers = []
        for name, module in self.named_children():
            if isinstance(module, nn.Sequential):
                first_level_sequential_layers.append((name, module))
        return first_level_sequential_layers

    def forward(self, x):
        """Calculate embeddings or logits given a batch of input image tensors.

        Arguments:
            x {torch.tensor} -- Batch of image tensors representing faces.

        Returns:
            torch.tensor -- Batch of embedding vectors or multinomial logits.
        """
        x = self.conv2d_1a(x)
        x = self.conv2d_2a(x)
        x = self.conv2d_2b(x)
        x = self.maxpool_3a(x)
        x = self.conv2d_3b(x)
        x = self.conv2d_4a(x)
        x = self.conv2d_4b(x)
        x = self.repeat_1(x)
        x = self.mixed_6a(x)
        x = self.repeat_2(x)
        x = self.mixed_7a(x)
        x = self.repeat_3(x)
        x = self.block8(x)
        x = self.avgpool_1a(x)
        x = self.dropout(x)
        x = self.last_linear(x.view(x.shape[0], -1))
        x = self.last_bn(x)
        if self.classify:
            x = self.logits(x)
        else:
            x = F.normalize(x, p=2, dim=1)
        return x


def load_weights(mdl, name):
    """Download pretrained state_dict and load into model.

    Arguments:
        mdl {torch.nn.Module} -- Pytorch model.
        name {str} -- Name of dataset that was used to generate pretrained state_dict.

    Raises:
        ValueError: If 'pretrained' not equal to 'vggface2' or 'casia-webface'.
    """
    if name == "vggface2":
        path = "https://github.com/timesler/facenet-pytorch/releases/download/v2.2.9/20180402-114759-vggface2.pt"
    elif name == "casia-webface":
        path = "https://github.com/timesler/facenet-pytorch/releases/download/v2.2.9/20180408-102900-casia-webface.pt"
    elif name.endswith(".pt"):
        path = name
        state_dict = torch.load(path)
        mdl.load_state_dict(state_dict)
        return
    else:
        raise ValueError(
            'Pretrained models only exist for "vggface2" and "casia-webface"'
        )

    model_dir = os.path.join(get_torch_home(), "checkpoints")
    os.makedirs(model_dir, exist_ok=True)

    cached_file = os.path.join(model_dir, os.path.basename(path))
    if not os.path.exists(cached_file):
        download_url_to_file(path, cached_file)

    state_dict = torch.load(cached_file, weights_only=True)
    mdl.load_state_dict(state_dict, strict=False)


def get_torch_home():
    torch_home = os.path.expanduser(
        os.getenv(
            "TORCH_HOME", os.path.join(os.getenv("XDG_CACHE_HOME", "~/.cache"), "torch")
        )
    )
    return torch_home
