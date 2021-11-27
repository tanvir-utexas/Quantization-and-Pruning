# Quantisation Functions
import pprint
from typing import Type, Dict, Any, Tuple, Iterable
import copy
import math
from collections import namedtuple
import numpy as np
import torch
import torch.nn as nn
import torch.fx as fx
import torch.nn.functional as F
import torch.optim as optim
from torchvision import datasets, transforms
from torch.utils.data import DataLoader
import torch.nn.functional as F
from torchvision.transforms import ToTensor, Lambda, Compose
import matplotlib.pyplot as plt
from Network import MyConvNet
import pandas as pd

import sys

QTensor = namedtuple('QTensor', ['tensor', 'scale', 'zero_point'])

conv1_zx = 0
conv1_zw = 122
conv1_zn = 136
conv1_M = 9
conv2_zx = 136
conv2_zw = 123
conv2_zn = 169
conv2_M = 8
lin1_zx = 169
lin1_zw = 121
lin1_zn = 132
lin1_M = 9
lin2_zx = 132
lin2_zw = 125
lin2_zn = 81
lin2_M = 12


def next_power_of_2(x):
    return 1 if x == 0 else 2**math.ceil(math.log2(x))

def calcScaleZeroPoint(min_val, max_val, num_bits=8):
    # Calc Scale and zero point of next
    qmin = 0.
    qmax = 2. ** num_bits - 1.

    scale = (max_val - min_val) / (qmax - qmin)

    initial_zero_point = qmin - min_val / scale

    zero_point = 0
    if initial_zero_point < qmin:
        zero_point = qmin
    elif initial_zero_point > qmax:
        zero_point = qmax
    else:
        zero_point = initial_zero_point

    zero_point = int(zero_point)

    return scale, zero_point


def quantize_tensor(x, num_bits=8, min_val=None, max_val=None):
    if not min_val and not max_val:
        min_val, max_val = x.min(), x.max()

    qmin = 0.
    qmax = 2. ** num_bits - 1.

    scale, zero_point = calcScaleZeroPoint(min_val, max_val, num_bits)
    q_x = zero_point + x / scale
    q_x.clamp_(qmin+50, qmax).round_()
    q_x = q_x.round()
    q_x = q_x.to(torch.int32)
    return QTensor(tensor=q_x, scale=scale, zero_point=zero_point)


def dequantize_tensor(q_x):
    return q_x.scale * (q_x.tensor.float() - q_x.zero_point)


# Rework Forward pass of Linear and Conv Layers to support Quantisation

def quantizeLayer(x, layer, stat, scale_x, zp_x, num_bits=8, weights_save_name=None):
    # for both conv and linear layers
    zp_x = eval(f'{weights_save_name}_zx')
    zp_w = eval(f'{weights_save_name}_zw')
    zp_n = eval(f'{weights_save_name}_zn')
    M = eval(f'{weights_save_name}_M')

    # cache old values
    W = layer.weight.data
    # quantise weights, activations are already quantised
    w = quantize_tensor(layer.weight.data, num_bits=num_bits)

    layer.weight.data = w.tensor

    # All int computation
    if 'conv' in weights_save_name:
        x = F.conv2d(x, layer.weight.data - zp_w, padding=1).to(torch.int32)
        x = x >> M
        # x = x + zp_n
    elif 'lin' in weights_save_name:
        x = F.linear(x, layer.weight.data - zp_w).to(torch.int32)
        x = x >> M
        # x = x + zp_n
    # Reset weights for next forward pass
    layer.weight.data = W
    # x = x.float()
    return x, None, zp_n, (None, zp_w)


'''Get Max and Min Stats for Quantising Activations of Network.
This is done by running the network with around 1000 examples 
and getting the average min and max activation values before and after each layer.'''


# Get Min and max of x tensor, and stores it
def updateStats(x, stats, key):
    max_val = torch.max(x)
    min_val = torch.min(x)

    if key not in stats:
        stats[key] = {"max": max_val.sum(), "min": min_val.sum(), "total": 1}
    else:
        stats[key]['max'] += max_val.sum().item()
        stats[key]['min'] += min_val.sum().item()
        stats[key]['total'] += 1

    return stats


# Reworked Forward Pass to access activation Stats through updateStats function
def gatherActivationStats(model, x, stats):
    stats = updateStats(x.clone().view(x.shape[0], -1), stats, 'conv1_before')
    x = model.conv1(x)
    stats = updateStats(x.clone().view(x.shape[0], -1), stats, 'conv1_after')

    x = model.act1(x)
    x = model.pool1(x)

    stats = updateStats(x.clone().view(x.shape[0], -1), stats, 'conv2_before')
    x = model.conv2(x)
    stats = updateStats(x.clone().view(x.shape[0], -1), stats, 'conv2_after')

    x = model.act2(x)
    x = model.pool2(x)

    x = x.view(x.size(0), -1)

    stats = updateStats(x, stats, 'lin1_before')
    x = model.lin1(x)

    stats = updateStats(x, stats, 'lin2_before')

    x = model.lin2(x)

    stats = updateStats(x, stats, 'lin2_after')
    return stats


# Entry function to get stats of all functions.
def gatherStats(model, test_loader):
    device = 'cpu'

    model.eval()
    test_loss = 0
    correct = 0
    stats = {}
    with torch.no_grad():
        for data, target in test_loader:
            data, target = data.to(device), target.to(device)
            stats = gatherActivationStats(model, data, stats)

    final_stats = {}
    for key, value in stats.items():
        final_stats[key] = {"max": value["max"] / value["total"], "min": value["min"] / value["total"]}
    return final_stats


def csv_img_tensor(tensor, path='./output_tensors/Random_Int_Input/', name = ''):
    """
    Function to convert conv tensor data to 2D CSV file
      
    Input tensor shape: (batch, ch, k, k)

    Store CSV: (k * k * ch * batch)

    """
    array = (tensor.data.cpu().detach().numpy())
    
    print("Operating with {}_output Tensor with shape {}".format(name, array.shape))

    array = array.reshape(array.shape[0], array.shape[1], -1) # (out, in, k, k) -> (out, in, k * k)

    final = []

    for i in range(len(array)):
        temp = [] # HOLDS [ k * k * in]
        
        for j in range(len(array[i])):
            temp.append(list(array[i][j]))
        
        temp = np.concatenate(temp)
        temp = list(temp)
        
        final.append(temp)
    
    final = np.concatenate(final)
    df = pd.DataFrame(final)
    df_transposed = df.T
    if 'int' in name:
        df_transposed = df_transposed.astype(int)
    df_transposed.to_csv(path + f"{name}_output.csv", index=False, header=False)


def csv_lin_tensor(tensor, path='./output_tensors/Random_Int_Input/', name = ''):
    """
    Function to convert conv tensor data to 2D CSV file
      
    Input tensor shape: (batch, features)

    Store CSV: (batch * features)

    """
    array = (tensor.data.cpu().detach().numpy())
    
#    array = array.reshape(array.shape[0], array.shape[1], -1) # (out, in, k, k) -> (out, in, k * k)

    print("Operating with {}_output Tensor with shape {}".format(name, array.shape))

    final = []
    
    for i in range(len(array)):    
        final.append(list(array[i]))
    
    final = np.concatenate(final)

    df = pd.DataFrame(final)
    
    df_transposed = df.T
    if 'int' in name:
        df_transposed = df_transposed.astype(int)
    df_transposed.to_csv(path + f"{name}_output.csv", index=False, header=False)

# Forward Pass for Quantised Inference

def quantForward(model, x, stats, quant_num_bits):
    # Quantise before inputting into incoming layers
    scale_zs_dict = {}

    ###Call a function to flatten tensor x
    ###Write the dataframe in csv
    ##input image
    x = torch.randn((1, 1, 28, 28))
    #print(x)
    # print(x.shape)
    x = quantize_tensor(x, num_bits=quant_num_bits, min_val=stats['conv1_before']['min'],
                        max_val=stats['conv1_before']['max'])

    print(x.tensor)
    print(x.tensor.shape)
  
    #x.tensor = torch.randint(low=50, high=100, size=(1, 1, 28, 28), requires_grad= True).to(torch.int32)
    
#    x.tensor = torch.add(x.tensor.data, a)

    csv_img_tensor(x.tensor, name = 'input')

    x, scale_next, zero_point_next, _ = quantizeLayer(x.tensor, model.conv1, stats['conv1_after'],
                                                   x.scale, x.zero_point, num_bits=quant_num_bits, weights_save_name='conv1')

    ###Call a function to flatten tensor x
    ###Write the dataframe in csv
    ##Output of conv1

    csv_img_tensor(x, name = 'conv1')

    x = torch.clamp(x, min=0)
    x = x.float()

    csv_img_tensor(x, name = 'relu1')

    x = model.pool1(x)
    x = x.to(torch.int32)

    csv_img_tensor(x, name = 'pool1')

    x, scale_next, zero_point_next, _ = quantizeLayer(x, model.conv2, stats['conv2_after'],
                                                                                    scale_next, zero_point_next, num_bits=quant_num_bits, weights_save_name='conv2')
    csv_img_tensor(x, name = 'conv2')

    x = torch.clamp(x, min=0)

    csv_img_tensor(x, name = 'relu2')

    x = x.float()
    x = model.pool2(x)
    x = x.to(torch.int32)

    csv_img_tensor(x, name = 'pool2')

    x = x.view(x.size(0), -1)

    csv_lin_tensor(x, name = "Faltten")

    x, scale_next, zero_point_next, _ = quantizeLayer(x, model.lin1, stats['lin2_before'],
                                                    scale_next, zero_point_next, num_bits=quant_num_bits, weights_save_name='lin1')

    csv_lin_tensor(x, name = "lin1")
    
    x, scale_next, zero_point_next, _ = quantizeLayer(x, model.lin2, stats['lin2_after'], scale_next, zero_point_next,
                                                   num_bits=quant_num_bits, weights_save_name='lin2')
    csv_lin_tensor(x, name = "lin2")
    
    return x


def NoQuantforward(model, x):
    c1 = model.conv1(x)
    b1 = model.bn1(c1)
    a1 = model.act1(b1)
    p1 = model.pool1(a1)
    c2 = model.conv2(p1)
    b2 = model.bn2(c2)
    a2 = model.act2(b2)
    p2 = model.pool2(a2)
    flt = p2.view(p2.size(0), -1)
    l1 = model.lin1(flt)
    out = model.lin2(l1)
    return out


def testQuant(model, test_loader, quant=False, stats=None, quant_num_bits=8):
    device = 'cpu'

    model.eval()
    test_loss = 0
    correct = 0
    with torch.no_grad():
        for data, target in test_loader:
            data, target = data.to(device), target.to(device)
            if quant:
                pred = quantForward(model, data, stats, quant_num_bits)
            else:
                pred = model(data)
            
            sys.exit()

            test_loss += 0
            correct += (pred.argmax(1) == target).type(torch.float).sum().item()
    test_loss /= len(test_loader.dataset)

    print('\nTest set: Average loss: {:.4f}, Accuracy: {}/{} ({:.0f}%)\n'.format(
        test_loss, correct, len(test_loader.dataset),
        100. * correct / len(test_loader.dataset)))

def fuse_conv_bn_eval(conv, bn):
    """
    Given a conv Module `A` and an batch_norm module `B`, returns a conv
    module `C` such that C(x) == B(A(x)) in inference mode.
    """
    assert(not (conv.training or bn.training)), "Fusion only for eval!"
    fused_conv = copy.deepcopy(conv)

    fused_conv.weight, fused_conv.bias = \
        fuse_conv_bn_weights(fused_conv.weight, fused_conv.bias,
                             bn.running_mean, bn.running_var, bn.eps, bn.weight, bn.bias)

    return fused_conv

def fuse_conv_bn_weights(conv_w, conv_b, bn_rm, bn_rv, bn_eps, bn_w, bn_b):
    if conv_b is None:
        conv_b = torch.zeros_like(bn_rm)
    if bn_w is None:
        bn_w = torch.ones_like(bn_rm)
    if bn_b is None:
        bn_b = torch.zeros_like(bn_rm)
    bn_var_rsqrt = torch.rsqrt(bn_rv + bn_eps)

    conv_w = conv_w * (bn_w * bn_var_rsqrt).reshape([-1] + [1] * (len(conv_w.shape) - 1))
    conv_b = (conv_b - bn_rm) * bn_var_rsqrt * bn_w + bn_b

    return torch.nn.Parameter(conv_w), torch.nn.Parameter(conv_b)

def _parent_name(target : str) -> Tuple[str, str]:
    """
    Splits a qualname into parent path and last atom.
    For example, `foo.bar.baz` -> (`foo.bar`, `baz`)
    """
    *parent, name = target.rsplit('.', 1)
    return parent[0] if parent else '', name

def replace_node_module(node: fx.Node, modules: Dict[str, Any], new_module: torch.nn.Module):
    assert(isinstance(node.target, str))
    parent_name, name = _parent_name(node.target)
    setattr(modules[parent_name], name, new_module)


def fuse(model: torch.nn.Module) -> torch.nn.Module:
    model = copy.deepcopy(model)
    # The first step of most FX passes is to symbolically trace our model to
    # obtain a `GraphModule`. This is a representation of our original model
    # that is functionally identical to our original model, except that we now
    # also have a graph representation of our forward pass.
    fx_model: fx.GraphModule = fx.symbolic_trace(model)
    modules = dict(fx_model.named_modules())

    # The primary representation for working with FX are the `Graph` and the
    # `Node`. Each `GraphModule` has a `Graph` associated with it - this
    # `Graph` is also what generates `GraphModule.code`.
    # The `Graph` itself is represented as a list of `Node` objects. Thus, to
    # iterate through all of the operations in our graph, we iterate over each
    # `Node` in our `Graph`.
    for node in fx_model.graph.nodes:
        # The FX IR contains several types of nodes, which generally represent
        # call sites to modules, functions, or methods. The type of node is
        # determined by `Node.op`.
        if node.op != 'call_module': # If our current node isn't calling a Module then we can ignore it.
            continue
        # For call sites, `Node.target` represents the module/function/method
        # that's being called. Here, we check `Node.target` to see if it's a
        # batch norm module, and then check `Node.args[0].target` to see if the
        # input `Node` is a convolution.
        if type(modules[node.target]) is nn.BatchNorm2d and type(modules[node.args[0].target]) is nn.Conv2d:
            if len(node.args[0].users) > 1:  # Output of conv is used by other nodes
                continue
            conv = modules[node.args[0].target]
            bn = modules[node.target]
            fused_conv = fuse_conv_bn_eval(conv, bn)
            replace_node_module(node.args[0], modules, fused_conv)
            # As we've folded the batch nor into the conv, we need to replace all uses
            # of the batch norm with the conv.
            node.replace_all_uses_with(node.args[0])
            # Now that all uses of the batch norm have been replaced, we can
            # safely remove the batch norm.
            fx_model.graph.erase_node(node)
    fx_model.graph.lint()
    # After we've modified our graph, we need to recompile our graph in order
    # to keep the generated code in sync.
    fx_model.recompile()

# if "__name__" == "__main__":
device = "cpu"
print("Using {} device".format(device))

# Download test data from open datasets.
test_data = datasets.FashionMNIST(
    root="data",
    train=False,
    download=True,
    transform=ToTensor(),
)


batch_size = 64
test_dataloader = DataLoader(test_data, batch_size=1)

# model_path = 'channel_0.4_filter_0.25_pruned_model.pth'
model_path = 'new_model.pth'
q_model = torch.load(model_path).to(device)
print("Loading model from {}".format(model_path))
q_model.eval()

loss_fn = nn.CrossEntropyLoss()
optimizer = torch.optim.SGD(q_model.parameters(), lr=1e-3)

stats = gatherStats(q_model, test_dataloader)
print(stats)

#test on quantized model
quant_num_bits = 8
testQuant(q_model, test_dataloader, quant=True, stats=stats, quant_num_bits=quant_num_bits)
