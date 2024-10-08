import os

import sys

import time
import torch
import torch._dynamo as dynamo
import logging

torch.set_float32_matmul_precision('high')
from typing import Dict, List, Tuple

import pickle
from .node import *

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

@torch.compile(fullgraph=True, mode='reduce-overhead')
def levelwiseSL(levels: List[torch.Tensor], idx2primesub: torch.Tensor, data: torch.Tensor, theta: torch.Tensor):
    for level in levels:
        theta[level] = data[idx2primesub[level]].sum(-2)
        data[level] = theta[level].logsumexp(-2)
        theta[level] -= data[level].unsqueeze(1)
    return data[levels[-1]]

@torch.compile(fullgraph=True, mode='reduce-overhead')
def levelwiseMars(levels: List[torch.Tensor], idx2primesub: torch.Tensor, data: torch.Tensor, theta: torch.Tensor, parents: torch.Tensor):
    for level in reversed(levels):
        data[level] = (theta[parents[level].unbind(-1)] + data[parents[level].unbind(-1)[0]]).logsumexp(-2)

@torch.compile(fullgraph=True, mode='reduce-overhead')
def log1mexp(x):
    # Source: https://github.com/wouterkool/estimating-gradients-without-replacement/blob/9d8bf8b/bernoulli/gumbel.py#L7-L11
    # Computes log(1-exp(-|x|))
    # See https://cran.r-project.org/web/packages/Rmpfr/vignettes/log1mexp-note.pdf
    x = -x.abs()
    x = torch.where(
        x > -0.6931471805599453094,
        torch.log(-torch.expm1(x)),
        torch.log1p(-torch.exp(x)),
    )

    return x

def levelOrder(beta):
    """
    :type root: Node
    :rtype: List[List[int]]
    """
    seen = dict()
    nodes = [beta]
    level = []
    answer = []
    result = [[beta]]
    while len(nodes) != 0:
        for a in nodes:
            if not a.is_decomposition():
                continue
            for element in a.elements:
                for e in element:
                    if not e.is_decomposition():
                        continue
                    if seen.get(e) != None: continue
                    seen[e] = True
                    level.append(e)
        nodes = level
        for i in level:
            answer.append(i)
        level = []
        answer = list(dict.fromkeys(answer))
        result.append(answer)
        answer = []
    return result[:-1]

@torch.compile(fullgraph=True, mode='reduce-overhead')
def gumbel_keys(w):
    # sample some gumbels
    uniform = torch.rand(w.shape, device="cuda")#.to(device)
    z = -torch.log(-torch.log(uniform))
    w = w + z
    return w

@torch.compile(fullgraph=True, mode='reduce-overhead')
def sample_subset(w, k):
    '''
    Args:
        w (Tensor): Float Tensor of weights for each element. In gumbel mode
            these are interpreted as log probabilities
        k (int): number of elements in the subset sample
    '''
    with torch.no_grad():
        w = gumbel_keys(w)
        return w.topk(k).indices

class Layer:
    
    def __init__(self):

        # Read in circuit
        with open('4C1.pkl', 'rb') as inp:
            beta = pickle.load(inp)

        max_elements = 0
        for node in beta.positive_iter():
            if node.is_decomposition():
                max_elements = max(max_elements, len(node.elements))

        print("max_elements", max_elements)

        levels_nodes = levelOrder(beta)

        # Reset ids
        nodes = [node for node in beta.positive_iter()]
        nodes = list(dict.fromkeys(nodes))

        id = 0
        for e in nodes:
            e.id = id
            id += 1 
        self.id = id

        from collections import defaultdict
        parents_dict = defaultdict(list)
        for node in beta.positive_iter():
            if node.is_decomposition():
                for i, (p, s) in enumerate(node.elements):
                    parents_dict[p.id] += [[node.id, i]]
                    parents_dict[s.id] += [[node.id, i]]


        # Set up the parents for an efficient backward pass
        max_parents = 0
        for p in parents_dict.values():
            max_parents = max(len(p), max_parents)

        parents = torch.empty((id, max_parents, 2), dtype=torch.int, device="cuda").fill_(id)#.to(device)
        for k,v in parents_dict.items():
            parents[k] = torch.tensor(v + [[id, 0]]*(max_parents - len(v)), dtype=torch.int, device='cuda')
        self.parents = parents

        # Levels
        levels = []
        for level in levels_nodes:
            levels.append(torch.tensor([l.id for l in level], dtype=torch.int, device="cuda"))#.to(device))
        levels.reverse()
        self.levels = levels

        print("Num. levels: ", len(levels))

        # true indices
        true_indices = torch.tensor([node.id for node in nodes if node.is_true()], dtype=torch.int, device="cuda")#.to(device)
        self.true_indices = true_indices

        # Literal indices
        literal_indices = torch.tensor([[node.id, node.literal] for node in nodes if node.is_literal()], dtype=torch.int, device='cuda')
        literal_indices, literal_mask = literal_indices.unbind(-1)
        literal_mask = literal_mask.abs() - 1, (literal_mask > 0).long()
        self.literal_indices = literal_indices
        self.literal_mask = literal_mask
        
        order = self.literal_mask[0][self.literal_mask[1].bool()].sort().indices
        self.pos_literals = self.literal_indices[self.literal_mask[1].bool()][order]

        # Map nodes to their primes/subs
        idx2primesub = torch.zeros((id, max_elements, 2), dtype=torch.int, device="cuda")#.to(device)
        for node in nodes:
            if node.is_decomposition():
                tmp = torch.tensor([[p.id, s.id] for p, s in node.elements], dtype=torch.int)
                idx2primesub[node.id] = torch.nn.functional.pad(tmp, (0,0,0, max_elements - len(tmp)), value=id)
        self.idx2primesub = idx2primesub

    def __call__(self, log_probs):
        samples = self.sample(log_probs)
        marginals = self.log_pr(log_probs).exp().permute(1,0)
        return (samples - marginals).detach() + marginals

    # to save memory, try commenting out the torch.compile decorator for the below function 
    @torch.compile(fullgraph=True, mode='reduce-overhead')
    def log_pr(self, log_probs):
        lit_weights = torch.stack((log1mexp(-log_probs.detach()), log_probs), dim=-1).permute(1, 2, 0)

        data = torch.empty(self.id+1, log_probs.size(0), device="cuda")
        theta = torch.zeros(self.id+1, self.idx2primesub.size(1), log_probs.size(0), device="cuda")

        data[self.true_indices] = 0
        data[self.id] =  -float(1000)
        data[self.literal_indices] = lit_weights[self.literal_mask[0], self.literal_mask[1]] 

        res = levelwiseSL(self.levels, self.idx2primesub, data, theta)
        data[self.levels[-1]] -= data[self.levels[-1]] 
        levelwiseMars([self.literal_indices] + self.levels[:-1], self.idx2primesub, data, theta, self.parents)
        return data[self.pos_literals]

    @torch.compile(fullgraph=True, mode='reduce-overhead')
    def sample(self, lit_weights, k=2):
        with torch.no_grad():
            samples = sample_subset(lit_weights, k)
            samples_hot = torch.zeros_like(lit_weights)
            samples_hot.scatter_(1, samples, 1)
            return samples_hot.float()

# if __name__ == '__main__':


    # from torch import log
    # torch.set_printoptions(precision=4, sci_mode=False)
    # dynamo.config.cache_size_limit=10000

    # from create_simple_constraint import create_exactly_k
    # alpha = create_exactly_k(4, 1)[0][-1]
    # with open('4C1.pkl', 'wb') as out:
    #     pickle.dump(alpha, out, pickle.HIGHEST_PROTOCOL)

    # # Prepare probabilities
    # batch_size = 1
    
    # # Create layer
    # global layer
    # layer = Layer()
    
    # for i in range(10):
    #     log_probs = probs = torch.tensor([[0.3, 0.6, 0.5, 0.2], [0.3, 0.6, 0.5, 0.2]], device='cuda', requires_grad=True).log()
    #     start = time.time()
    #     marginals = layer.log_pr(log_probs).permute(1,0).exp()
    #     gt = torch.tensor([[0.3568, 0.7622, 0.6595, 0.2216]], device='cuda').expand(2, 4)
    #     assert(torch.isclose(marginals, gt, rtol=1e-03).all())
    #     samples = layer(log_probs)
    #     samples.sum().backward()
    #     print("elapsed time:", time.time() - start)