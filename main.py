# coding: utf-8

import os
import json

from mig_gt.configs.default_config import add_arguments_by_config_class, combine_args_into_config
from mig_gt.configs.mm_mgdcf_default_config import MMMGDCFConfig
from mig_gt.configs.masked_mm_mgdcf_default_config import load_masked_mm_mgdcf_default_config 
import sys
import argparse
import time
import torch.nn.functional as F

from mig_gt.layers.mirf_gt import MIGGT
from mig_gt.vector_search.vector_search import VectorSearchEngine




parser = argparse.ArgumentParser()
parser.add_argument('--dataset', type=str, default='baby', help='name of datasets')
parser.add_argument('--method', type=str)
parser.add_argument('--result_dir', type=str)
parser.add_argument('--seed', type=int)
parser.add_argument('--gpu', type=str)


config_class = MMMGDCFConfig
parser = add_arguments_by_config_class(parser, config_class)
args = parser.parse_args()

config = load_masked_mm_mgdcf_default_config(args.dataset)
config = combine_args_into_config(config, args)

print(config)

os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu


from mig_gt.utils.random_utils import reset_seed 
reset_seed(args.seed)

from mig_gt.layers.mm_mgdcf import MMMGDCF
import shortuuid
from mig_gt.layers.sign import random_project, sign_pre_compute
from mig_gt.losses import compute_info_bpr_loss, compute_l2_loss
from mig_gt.utils.data_loader_utils import create_tensors_dataloader
from mig_gt.evaluation.ranking import evaluate_mean_global_metrics
from mig_gt.layers.mgdcf import MGDCF
from mig_gt.load_data import load_data
import torch
import numpy as np
import dgl
import dgl.function as fn
import time
import torch.nn as nn
from dataclasses import asdict



embedding_size = config.embedding_size 



device = "cuda"



train_user_item_edges, valid_user_item_edges, test_user_item_edges, train_user_items_dict, train_mask_user_items_dict, valid_user_items_dict, valid_mask_user_items_dict, test_user_items_dict, test_mask_user_items_dict, num_users, num_items, v_feat, t_feat = load_data(args.dataset)

start_time = time.time()


run_id = shortuuid.uuid()


result_dir = args.result_dir

if not os.path.exists(result_dir):
    os.makedirs(result_dir)

result_path = os.path.join(result_dir, "{}.json".format(run_id))
tmp_result_path = os.path.join(result_dir, "{}.json.tmp".format(run_id))



if config.use_rp:
    v_feat = random_project(v_feat, t_feat.size(-1))



num_train_user_item_edges = len(train_user_item_edges)
g = MGDCF.build_sorted_homo_graph(train_user_item_edges, num_users=num_users, num_items=num_items).to(device)
assert g.num_edges() == num_train_user_item_edges * 2 + num_users + num_items


num_nodes = g.num_nodes()

degs = g.in_degrees().to(device)

item_item_g = None



    

v_feat = v_feat.to(device)
t_feat = t_feat.to(device)




user_embeddings = np.random.randn(num_users, embedding_size) / np.sqrt(embedding_size)
user_embeddings = torch.tensor(user_embeddings, dtype=torch.float32, requires_grad=True, device=device)


item_embeddings = np.random.randn(num_items, embedding_size) / np.sqrt(embedding_size)
item_embeddings = torch.tensor(item_embeddings, dtype=torch.float32, requires_grad=True, device=device)



method = args.method

if method == "mig":

    model = MMMGDCF(
        k_e=config.k_e,
        k_t=config.k_t,
        k_v=config.k_v,

        alpha=config.alpha, 
        beta=config.beta, 

        input_feat_drop_rate=config.input_feat_drop_rate,
        feat_drop_rate=config.feat_drop_rate,
        user_x_drop_rate=config.user_x_drop_rate,
        item_x_drop_rate=config.item_x_drop_rate, 
        edge_drop_rate=config.edge_drop_rate, 
        z_drop_rate=config.z_drop_rate,
        item_v_in_channels=v_feat.size(-1),
        item_v_hidden_channels_list=[config.feat_hidden_units, embedding_size], 
        item_t_in_channels=t_feat.size(-1), 
        item_t_hidden_channels_list=[config.feat_hidden_units, embedding_size], 
        bn=config.bn,
    ).to(device)

elif method == "mig_gt":

    model = MIGGT(
        # k=config.k,

        k_e=config.k_e,
        k_t=config.k_t,
        k_v=config.k_v,

        alpha=config.alpha, 
        beta=config.beta, 

        input_feat_drop_rate=config.input_feat_drop_rate,
        feat_drop_rate=config.feat_drop_rate,
        user_x_drop_rate=config.user_x_drop_rate,
        item_x_drop_rate=config.item_x_drop_rate, 
        edge_drop_rate=config.edge_drop_rate, 
        z_drop_rate=config.z_drop_rate,
        user_in_channels=config.embedding_size,
        item_v_in_channels=v_feat.size(-1),
        item_v_hidden_channels_list=[config.feat_hidden_units, embedding_size], 
        item_t_in_channels=t_feat.size(-1), 
        item_t_hidden_channels_list=[config.feat_hidden_units, embedding_size], 

        bn=config.bn,
        num_clusters=config.num_clusters,
        num_samples=config.num_samples
    ).to(device)


use_clip_loss = False
use_mm_mf_loss = False



def forward(g, return_all=False):
    if return_all:
        virtual_h, emb_h, t_h, v_h, encoded_t, encoded_v, z_memory_h = model(g, user_embeddings, v_feat, t_feat, 
                                                                 item_embeddings=item_embeddings if config.use_item_emb else None, 
                                                                 return_all=return_all)
    else:
        virtual_h = model(g, user_embeddings, v_feat, t_feat, item_embeddings=item_embeddings if config.use_item_emb else None, 
                          return_all=return_all)
    user_h = virtual_h[:num_users]
    item_h = virtual_h[num_users:]

    if return_all:

        user_emb_h = emb_h[:num_users]
        item_emb_h = emb_h[num_users:]

        user_t_h = t_h[:num_users]
        item_t_h = t_h[num_users:]

        if v_h is not None:
            user_v_h = v_h[:num_users]
            item_v_h = v_h[num_users:]
        else:
            user_v_h = None
            item_v_h = None

        return user_h, item_h, user_emb_h, item_emb_h, user_t_h, item_t_h, user_v_h, item_v_h, encoded_t, encoded_v, z_memory_h
    else:
        return user_h, item_h
    

        



def evaluate(user_items_dict, mask_user_items_dict):
    model.eval()
    user_h, item_h = forward(g)
    user_h = user_h.detach().cpu().numpy()
    item_h = item_h.detach().cpu().numpy()

    mean_results_dict = evaluate_mean_global_metrics(user_items_dict, mask_user_items_dict,
                                                    user_h, item_h, k_list=[10, 20], metrics=["precision","recall", "ndcg"])
    return mean_results_dict


def update_learning_rate(optimizer):
    for param_group in optimizer.param_groups:
        new_lr = param_group['lr'] * config.lr_decay
        if new_lr >= config.lr_decay_min:
            param_group['lr'] = new_lr
#         param_group['lr'] = param_group['lr'] * lr_decay



train_edges_data_loader = create_tensors_dataloader(
        torch.arange(len(train_user_item_edges)),
        torch.tensor(train_user_item_edges), batch_size=config.batch_size, shuffle=True
)


optimizer = torch.optim.Adam([user_embeddings, item_embeddings] + list(model.parameters()), lr=config.lr)

# early_stop_metric = "ndcg@20"
early_stop_metric = "recall@20"
best_valid_score = 0.0 
early_stop_valid_results_dict = None
early_stop_test_results_dict = None
best_epoch = None


combined_config_dict = vars(args)
for k, v in asdict(config).items():
    combined_config_dict[k] = v


patience_count = 0
total_train_time = 0.0

run_log_dir = "run_logs"
run_log_fname = "{}.json".format(args.dataset)
run_log_fpath = os.path.join(run_log_dir, run_log_fname)


for epoch in range(1, config.num_epochs + 1):

    epoch_start_time = time.time()

    for step, (batch_edge_indices, batch_edges) in enumerate(train_edges_data_loader):
        step_start_time = time.time()
        model.train()

      

        with g.local_scope():

            new_g = g

            if method == "mig":
                user_h, item_h = forward(new_g)
            else:
                user_h, item_h, user_emb_h, item_emb_h, user_t_h, item_t_h, user_v_h, item_v_h, encoded_t, encoded_v, z_memory_h = forward(new_g, return_all=True)


            # infobpr = bpr by default
            mf_losses = compute_info_bpr_loss(user_h, item_h, batch_edges, num_negs=config.num_negs, reduction="none")
        
            l2_loss = compute_l2_loss([user_h, item_h])

            loss = mf_losses.sum() + l2_loss * config.l2_coef




            if method != "mig":
                pos_user_h = user_h[batch_edges[:, 0]]
                pos_z_memory_h = z_memory_h[batch_edges[:, 1] + num_users]  
                unsmooth_logits = (pos_user_h.unsqueeze(1) @ pos_z_memory_h.permute(0, 2, 1)).squeeze(1)
                unsmooth_loss = F.cross_entropy(unsmooth_logits, torch.zeros([batch_edges.size(0)], dtype=torch.long, device=device), reduction="none").sum()
                loss = loss + unsmooth_loss


            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            step_end_time = time.time()

    update_learning_rate(optimizer)

    epoch_end_time = time.time()
    total_train_time += epoch_end_time - epoch_start_time

  

    print("epoch = {}\tloss = {:.4f}\tmf_loss = {:.4f}\tl2_loss = {:.4f}\tupdated_lr = {:.4f}\tepoch_time = {:.4f}s\tpcount = {}"
          .format(epoch, loss.item(), mf_losses.mean().item(), l2_loss.item(), optimizer.param_groups[0]['lr'], epoch_end_time-epoch_start_time, patience_count))
    

    if epoch % config.validation_freq == 0:
        print("\nEvaluation before epoch {} ......".format(epoch))

        valid_results_dict = evaluate(valid_user_items_dict, valid_mask_user_items_dict)
        print("valid_results_dict = ", valid_results_dict)


        current_score = valid_results_dict[early_stop_metric]
        if current_score > best_valid_score:

            test_results_dict = evaluate(test_user_items_dict, test_mask_user_items_dict)
            print("test_results_dict = ", test_results_dict)

            best_valid_score = current_score
            best_epoch = epoch
            early_stop_valid_results_dict = valid_results_dict
            early_stop_test_results_dict = test_results_dict

            print("updated early_stop_test_results_dict = ", early_stop_test_results_dict)
            patience_count = 0
        else:
            print("old early_stop_test_results_dict = ", early_stop_test_results_dict)
            patience_count += config.validation_freq

            if patience_count >= config.patience:
                print("Early stopping at epoch {} ......".format(epoch))
                break
        


