import os
import argparse
import time
import matplotlib.pyplot as plt
import cv2
cv2.setNumThreads(0)
import logging
import numpy as np
import scipy.sparse as ssp
import torch
import torch.nn.functional as F
from PIL import ExifTags, Image, ImageOps, ImageDraw, ImageFont
from torch.utils.data import DataLoader, Dataset, dataloader, distributed
import torchvision
from torchvision.models.detection.faster_rcnn import FastRCNNPredictor
from pycocotools.coco import COCO
import transforms as T
from tool.utils import *
from data import *
from engine import train_one_epoch, evaluate
from metric import *
from tensorized_GNN import BatchedGNNlayer

'''
Parcing arguments
'''

parser = argparse.ArgumentParser('argument for training')

parser.add_argument('--root_dir', type=str, help='path to dataset')

parser.add_argument('--seed', type=int, default=0, help='random seed')
parser.add_argument('--print_freq', type=int, default=5, help='print frequency')
parser.add_argument('--save_freq', type=int, default=4, help='save frequency')
parser.add_argument('--save_fig', type=bool, default=True, help='save figure')
parser.add_argument('--img_size', type=int, default=608, help='image size')
parser.add_argument('--batch_size', type=int, default=32, help='batch_size')
parser.add_argument('--num_workers', type=int, default=1, help='num of workers to use')
parser.add_argument('--epochs', type=int, default=30, help='number of training epochs')
parser.add_argument('--samp', type=int, default=0, help='sampling number')

# optimization
parser.add_argument('--learning_rate', type=float, default=0.001, help='learning rate')
parser.add_argument('--weight_decay', type=float, default=1e-4, help='weight decay')
parser.add_argument('--sgd_momentum', type=float, default=0.9, help='SGD momentum')
parser.add_argument('--stepsize', nargs='+', type=int, default=[16,22], help="Please give a stepsize for lr scheduler.")
parser.add_argument('--gamma', type=float, default=0.1,help="Please give a gamma for lr scheduler.")

# H2D
parser.add_argument('--L', type=int, default=3, help='The number of k-hops for considering the multi-hop neighbour aggregation')
parser.add_argument('--beta', type=float, default=1, help='The decaying parameter for considering the multi-hop neighbour aggregation')
parser.add_argument('--n_layers', type=int, default=2, help='The number of GNN layers for the node-feature learning')
parser.add_argument('--hidden_dim', type=int, default=512, help='Hidden embedding dimension for the predict layers')
parser.add_argument('--feat_dim', type=int, default=12544, help='Feature dimension for the Roi align output (e.g. 7*7*256 for the FPN)')
parser.add_argument('--numk', type=int, default=64, help='The number of anchor points')
parser.add_argument('--temp_t', type=float, default=0.001)
parser.add_argument('--temp_s', type=float, default=0.001)
parser.add_argument('--momentum', type=float, default=0.99)

# GPU setting
parser.add_argument('--gpu', default=0, type=int, help='GPU id to use.')
parser.add_argument('--device_ids', nargs='+', type=int, default=None, help="Please give a device list for parallel processing.")
parser.add_argument('--resume', default='', type=str,
                        help='path to latest checkpoint (default: none)')

# paths to checkpoint saving
parser.add_argument('--checkpoint_path', default='./output_h2d_gnn/', type=str,
                    help='where to save checkpoints. ')

# 
opt = parser.parse_args()

'''
Define the transformation

reference: MOCO v2, and ISD
'''
def get_transformA(image_size=608,format='pascal_voc'):
    t = A.Compose([
    A.LongestMaxSize(max_size=image_size),
    A.PadIfNeeded(min_height=image_size, min_width=image_size,border_mode=cv2.BORDER_CONSTANT),  
    A.RandomResizedCrop (608, 608, scale=(0.2, 1.), p=0.5),
    A.ColorJitter(brightness=0.4, contrast=0.4, saturation=0.4, hue=0.1, p=0.8),
    # grayscale
    A.ToGray(p=0.2),
    # blur
    A.GaussianBlur(sigma_limit=(.1,2.), p=0.5),    
    # 수평 뒤집기
    A.HorizontalFlip(p=0.5),
    # normalize
    A.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225], max_pixel_value=255)
    ],
        bbox_params=A.BboxParams(format='pascal_voc',min_visibility=0.5,label_fields=['class_labels'])
    )
    
    return t

'''
coco data
'''

class cocoDataSet(Dataset):
    def __init__(self, root_dir, set_name='train2017',transforms=None):
        super().__init__()
        self.root_dir = root_dir
        self.set_name = set_name
        self.transforms = transforms
        self.coco = COCO(os.path.join(self.root_dir, 'annotations', 'instances_' + self.set_name + '.json'))
        
        whole_image_ids = self.coco.getImgIds()  # remove the images which has no annot
        self.image_ids = []
        for idx in whole_image_ids:
            annotations_ids = self.coco.getAnnIds(imgIds=idx, iscrowd=False)
            if len(annotations_ids) != 0:
                self.image_ids.append(idx)
                
        self.tuples = self.generate_tuple_queues(len(self.image_ids))
      
    # gererate data tuple list: [{img1, instance1},{img1,instance2}, ... , {imgN, instanceK}]
    def generate_tuple_queues(self,n_imgs):
        
        memory_querys = []
        idx = 0
        for img in range(n_imgs):
            lab = self.load_annotations(img)
            nins = np.shape(lab)[0]
            for ins in range(nins):
                memory_querys.append([idx,ins])
            idx += 1
            
        return memory_querys
    
    # stacking the image and labels according to the data tuple
    def set_data_from_tuples(self,tup):
        
        img = self.load_image(tup[0])
        lab = self.load_annotations(tup[0])
        bbox = lab[tup[1],:]

        # transform for the query tuple
        if self.transforms is not None:
            while True:
                augmentations = self.transforms(image=img, bboxes=[list(bbox)], class_labels=[1])
                img_tmp = augmentations['image']
                bbox_tmp = np.array(augmentations['bboxes'])
                if len(bbox_tmp) > 0: # if the instance was excluded from the image (e.g. near the boundary): retry the transform
                    img = img_tmp
                    bbox = bbox_tmp
                    break

        img = img.transpose((2, 0, 1))[::-1]  # HWC to CHW, BGR to RGB
        img = torch.from_numpy(np.ascontiguousarray(img))
            
        return img, bbox       
    
    def load_image(self, image_index):
            image_info = self.coco.loadImgs(self.image_ids[image_index])[0]
            path = os.path.join(self.root_dir, self.set_name, image_info['file_name'])
            image = Image.open(path).convert('RGB')
            return np.array(image)
        
    def load_annotations(self, image_index):
        # get ground truth annotations
        annotations_ids = self.coco.getAnnIds(imgIds=self.image_ids[image_index], iscrowd=False)
        annotations = np.zeros((0, 4))

        # some images appear to miss annotations (like image with id 257034)
        if len(annotations_ids) == 0:
            return annotations

        # parse annotations
        coco_annotations = self.coco.loadAnns(annotations_ids)
        for idx, a in enumerate(coco_annotations):

            # some annotations have basically no width / height, skip them
            if a['bbox'][2] < 1 or a['bbox'][3] < 1:
                continue

            annotation = np.zeros((1, 4))
            annotation[0, :] = a['bbox']
            annotations = np.append(annotations, annotation, axis=0)

        # transform from [x, y, w, h] to [x1, y1, x2, y2]
        annotations[:, 2] = annotations[:, 0] + annotations[:, 2]
        annotations[:, 3] = annotations[:, 1] + annotations[:, 3]

        return annotations
    
    def __getitem__(self, idx):
        
        # get a key tuple 
        k_img, k_bbox = self.set_data_from_tuples(self.tuples[idx])
        q_img, q_bbox = self.set_data_from_tuples(self.tuples[idx])

        return k_img, q_img, k_bbox, q_bbox
        
    def __len__(self):
        return len(self.tuples)

'''
model

Higher-order Heuristics Distillation (H2D)
'''
class H2D(torch.nn.Module):
    def __init__(self,key_model,query_model,
                 hidden_dim=1024,feat_dim=12544,
                 m=0.99,K=1024,
                 L=2,decay=0.1,n_layers=2):
        super(H2D,self).__init__()
        
        self.feat_dim = feat_dim # feature dimension of the featuremaps from the backbone network
        self.hidden_dim = hidden_dim 
        self.m = m # for the momentum update
        self.K = K # the number of the anchors
        self.L = L # the radius for the structural aggregation
        self.decay = decay # decay rates for a larger neighbors
        self.n_layers = n_layers
        self.key_model = key_model
        self.query_model = query_model

        # prediction layer for the query tuples (for student)
        self.predict_q = torch.nn.Sequential(
            torch.nn.Linear(self.feat_dim,self.hidden_dim,bias=False),
            torch.nn.BatchNorm1d(self.hidden_dim),
            torch.nn.ReLU(inplace=True),
            torch.nn.Linear(self.hidden_dim,self.hidden_dim,bias=True),
        )
        
        # prediction layer for the key tuple (for teacher,)
        self.predict_k = torch.nn.Sequential(
            torch.nn.Linear(self.feat_dim,self.hidden_dim,bias=False),
            torch.nn.BatchNorm1d(self.hidden_dim),
            torch.nn.ReLU(inplace=True),
            torch.nn.Linear(self.hidden_dim,self.hidden_dim,bias=True),
        )
        
        # learning the structural information
        self.f_edge = torch.nn.Sequential(torch.nn.Linear(1, self.hidden_dim),
                                            torch.nn.ReLU(),
                                            torch.nn.Linear(self.hidden_dim, 1))

        self.f_node = torch.nn.Sequential(torch.nn.Linear(1, self.hidden_dim),
                                          torch.nn.ReLU(),
                                          torch.nn.Linear(self.hidden_dim, 1))

        self.g_phi = torch.nn.Sequential(torch.nn.Linear(1, self.hidden_dim),
                                         torch.nn.ReLU(),
                                         torch.nn.Linear(self.hidden_dim, 1))
        
        # gnn layers for the node-featrue-based learning
        self.gnns = torch.nn.ModuleList([BatchedGNNlayer(self.hidden_dim,self.hidden_dim)])
        if self.n_layers > 2:
            for _ in range(self.n_layers-2):
                self.gnns.append(BatchedGNNlayer(self.hidden_dim,self.hidden_dim))
        self.gnns.append(BatchedGNNlayer(self.hidden_dim,self.K + 2))
        
        self.alpha = torch.nn.Parameter(torch.FloatTensor([0, 0]))
        
        for param_q, param_k in zip(self.query_model.parameters(), self.key_model.parameters()):
            param_k.data.copy_(param_q.data)
            param_k.requires_grad = False
          
        for param_q, param_k in zip(self.predict_q.parameters(), self.predict_k.parameters()):
            param_k.data.copy_(param_q.data)
            param_k.requires_grad = False
           
        # setup queue
        self.register_buffer('queue', torch.randn(self.K, self.hidden_dim))
        # normalize the queue
        self.queue = F.normalize(self.queue, dim=1)
        print(self.queue.shape)

        # setup the queue pointer
        self.register_buffer('queue_ptr', torch.zeros(1, dtype=torch.long))
            
    @torch.no_grad()
    def _momentum_update_key_encoder(self):
        for param_q, param_k in zip(self.query_model.parameters(), self.key_model.parameters()):
            param_k.data = param_k.data * self.m + param_q.data * (1. - self.m)
            
    @torch.no_grad()
    def _momentum_update_key_predictor(self):
        for param_q, param_k in zip(self.predict_q.parameters(), self.predict_k.parameters()):
            param_k.data = param_k.data * self.m + param_q.data * (1. - self.m)
       
    @torch.no_grad()
    def data_parallel(self,ids,orig_id):
        self.query_model = torch.nn.DataParallel(self.query_model,device_ids=ids,output_device=orig_id)
        self.key_model = torch.nn.DataParallel(self.key_model,device_ids=ids,output_device=orig_id)
        self.predict_q = torch.nn.DataParallel(self.predict_q,device_ids=ids,output_device=orig_id)
        self.predict_k = torch.nn.DataParallel(self.predict_k,device_ids=ids,output_device=orig_id)
        for idx in range(len(self.gnns)):
            self.gnns[idx] = torch.nn.DataParallel(self.gnns[idx],device_ids=ids,output_device=orig_id)
        self.f_edge = torch.nn.DataParallel(self.f_edge,device_ids=ids,output_device=orig_id)
        self.f_node = torch.nn.DataParallel(self.f_node,device_ids=ids,output_device=orig_id)
        self.g_phi = torch.nn.DataParallel(self.g_phi,device_ids=ids,output_device=orig_id)
        
    # for the memory bank
    @torch.no_grad()
    def _dequeue_and_enqueue(self, keys):
        batch_size = keys.shape[0]

        ptr = int(self.queue_ptr)
        assert self.K % batch_size == 0

        # replace the keys at ptr (dequeue and enqueue)
        self.queue[ptr:ptr + batch_size] = keys
        ptr = (ptr + batch_size) % self.K  # move pointer

        self.queue_ptr[0] = ptr
       
    # RoiPooling using the multiscale outputs from the backbone (usually the FPN) with the scale index.
    def _batch_roi_align(self, multiscale_batch_output,batch_bboxex,k0=4,W=608,H=608,out_dim=7):

        # match the scale by using the bbox information
        w,h = batch_bboxex[:,0,2] - batch_bboxex[:,0,0], batch_bboxex[:,0,3] - batch_bboxex[:,0,1]
        val = torch.round(torch.log2(torch.sqrt( (w*h) / (W*H) )) + k0) 
        val[val<=2] = 2

        pooled=[]
        for batch in range(batch_bboxex.size(0)):
            lev = str(int(val[batch]-2))
            bbox = batch_bboxex[batch,0,:].unsqueeze(0)
            output = multiscale_batch_output[lev]
            pooled.append(torchvision.ops.roi_align(output,[bbox.float()],out_dim,output.size(-1)/W,2))

        return torch.vstack(pooled) # batch,channel,7,7

    def forward(self, k_img, q_img, k_bbox, q_bbox):
        
        nb = k_img.size(0) # batchsize
        
        # query features in the student model
        out_q = self.query_model(q_img) # multi-scale output
        aligned_q = self._batch_roi_align(out_q,q_bbox).view(nb,-1) # batch,feat_dim
        q = self.predict_q(aligned_q) # batch,hidden_dim
        q = F.normalize(q, dim=1) 
        
        # compute key features in the teacher model
        with torch.no_grad():
            self._momentum_update_key_encoder()
            self._momentum_update_key_predictor()
            
            # shuffle keys
            shuffle_ids, reverse_ids = get_shuffle_ids(k_img.shape[0])
            k_img = k_img[shuffle_ids]
            k_bbox = k_bbox[shuffle_ids]
            
            # embed the key feature vector
            out_k = self.key_model(k_img)
            aligned_k = self._batch_roi_align(out_k,k_bbox).view(nb,-1)
            k = self.predict_k(aligned_k)
            k = F.normalize(k, dim=1) # batch,hidden_dim

            # undo shuffle
            k = k[reverse_ids]
        
        # construct the feature matrix
        queue = self.queue.clone().detach() # K,hidden_dim
        batch_queue = queue.unsqueeze(0).repeat(nb,1,1) # batch,K,hidden_dim
        qk = torch.cat((q.unsqueeze(1),k.unsqueeze(1)),dim=1)
        x = torch.cat((qk,batch_queue),dim=1) # batch,(K+2),hidden_dim
        
        # adjacency matrix
        sym = torch.sigmoid(torch.bmm(x,x.transpose(-1,-2))) # batch,(K+2),(K+2)
        
        # remove the edge between query and key nodes
        # similar to the link prediction case
        sym[0,1] = 0
        sym[1,0] = 0
        adj = (sym >= 0.5).float() * 1
        
        # node features 
        for gcn in self.gnns:
            x = gcn(x,adj) # batch,(K+2),(K+2)
        X_feat = F.normalize(x, dim=2)
        
        # Structural information embedding
        h_e = self.f_edge(adj.unsqueeze(-1)) # batch, (K+2),(K+2), 1
        h_e = torch.sum(h_e,dim=-2) # batch, (K+2), 1
        h_n = self.f_node(h_e) # batch, (K+2), 1
        X_struct = torch.diag_embed(h_n.squeeze(-1)) # batch,(K+2),(K+2)
        
        # one hop Neigbourhood aggregation
        X_aggr = torch.bmm(adj,X_struct) # batch,(K+2),(K+2)
        
        # larger radius
        if self.L > 1:
            for l in range(self.L-1):
                adj = torch.bmm(adj,adj)
                X_aggr = X_aggr + self.decay * torch.bmm(adj,X_struct)
                self.decay = self.decay * self.decay
                
        # scale the output
        X_rep = self.g_phi(X_aggr.unsqueeze(-1)).squeeze(-1) # batch,(K+2),(K+2)
        
        # convex combination
        alpha = torch.softmax(self.alpha, dim=0)
        X_com = alpha[0]*X_feat + alpha[1]*X_rep # batch,(K+2),(K+2)
        
        # get target node info's
        dist_q = X_com[:,0,:] # batch,out_dim 
        dist_k = X_com[:,1,:] # batch,out_dim 
        
        # dequeue and enqueue
        self._dequeue_and_enqueue(k)
        
        return dist_q, dist_k
    
def get_shuffle_ids(bsz):
    """generate shuffle ids for ShuffleBN"""
    forward_inds = torch.randperm(bsz).long().cuda()
    backward_inds = torch.zeros(bsz).long().cuda()
    value = torch.arange(bsz).long().cuda()
    backward_inds.index_copy_(0, forward_inds, value)
    return forward_inds, backward_inds    
'''
training
'''    
def train_epoch(epoch, loader, model, criterion, optimizer, opt):
    
    model.train()
    
    batch_time = AverageMeter()
    data_time = AverageMeter()
    loss_meter = AverageMeter() # for the distillation loss
    
    end = time.time()
    for idx, (k_img, q_img, k_bbox, q_bbox) in enumerate(loader):
        data_time.update(time.time() - end)

        k_img = k_img.cuda(non_blocking=True)
        q_img = q_img.cuda(non_blocking=True)
        k_bbox = k_bbox.cuda(non_blocking=True)
        q_bbox = q_bbox.cuda(non_blocking=True)
        
        # forward
        sim_q, sim_k = model(k_img, q_img, k_bbox, q_bbox)
        loss = criterion(sim_q,sim_k) 

        # backward
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        
        #
        loss_meter.update(loss.item(), k_img.size(0))
        torch.cuda.synchronize()
        batch_time.update(time.time() - end)
        end = time.time()
        
        # print info
        if (idx + 1) % opt.print_freq == 0:
            print('Train: [{0}][{1}/{2}]\t'
                  'KLdiv Loss {loss.val:.3f} ({loss.avg:.3f})\t'.format(
                   epoch, idx + 1, len(loader),loss=loss_meter))
            sys.stdout.flush()
            
    return loss_meter.avg   

"""
    Loss funcs
"""
class KLD(torch.nn.Module):
    
    def __init__(self, temp_t, temp_s):
        super(KLD, self).__init__()
        self.temp_t = temp_t
        self.temp_s = temp_s

    def forward(self, inputs, targets):
        inputs = F.log_softmax(F.normalize(inputs,dim=1)/self.temp_s, dim=1)
        targets = F.softmax(F.normalize(targets,dim=1)/self.temp_t, dim=1)
        return F.kl_div(inputs, targets, reduction='batchmean')
    
"""
    GPU Setup
"""
def gpu_setup(use_gpu, gpu_id, device_ids):
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    if device_ids is None:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_id)  
    else:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(device_ids).replace('[','').replace(']','')  

    if torch.cuda.is_available() and use_gpu:
        print('cuda available with GPU:',torch.cuda.get_device_name(0))
        device = torch.device("cuda")
    else:
        print('cuda not available')
        device = torch.device("cpu")
    return device

"""
    main
""" 
def main(opt):
    
    torch.manual_seed(opt.seed)
    # cuda setting
    gpu_setup(True,opt.gpu,opt.device_ids)
    
    # training dataloader
    dataset = cocoDataSet(opt.root_dir, set_name='train2017',transforms=get_transformA())
    loader = torch.utils.data.DataLoader(
            dataset, batch_size=opt.batch_size, shuffle=True, num_workers=opt.num_workers,
            pin_memory=True, drop_last=True)
    
    # model initialization
    big_model = torchvision.models.detection.fasterrcnn_resnet50_fpn(pretrained=True) # for the faster-RCNN model
    key_model = big_model.backbone # the backbone with fpn
    query_model = big_model.backbone

    model = H2D(key_model,query_model,
                hidden_dim=opt.hidden_dim, feat_dim=opt.feat_dim, # in the original faster-RCNN, ROIalign calculate the feature map with size of (batch,256,7,7)
                m=opt.momentum,K=opt.numk,
                L = opt.L, decay=opt.beta)
    if opt.device_ids is not None:
        print('Use multi-gpu setting: {}'.format(opt.device_ids))
        model.data_parallel(opt.device_ids,opt.gpu)
    model = model.cuda()
    
    #criterion = torch.nn.KLDivLoss(reduction="batchmean")
    criterion = KLD(opt.temp_t,opt.temp_s)
    
    # optimizer
    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.SGD(params,
                                lr=opt.learning_rate,
                                momentum=opt.sgd_momentum,
                                weight_decay=opt.weight_decay)
    lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=opt.stepsize, gamma=opt.gamma)
    
    opt.start_epoch = 1
    if opt.resume:
        print('==> resume from checkpoint: {}'.format(opt.resume))
        ckpt = torch.load(opt.resume)
        print('==> resume from epoch: {}'.format(ckpt['epoch']))
        model.load_state_dict(ckpt['state_dict'], strict=True)
        optimizer.load_state_dict(ckpt['optimizer'])
        opt.start_epoch = ckpt['epoch'] + 1
    
    loss_epochs = []
    for epoch in range(opt.start_epoch, opt.epochs + 1):
        print("==> training...")
        time1 = time.time()
        
        loss_epoch = train_epoch(epoch, loader, model, criterion, optimizer, opt)
        loss_epochs.append(loss_epoch)
        lr_scheduler.step()

        time2 = time.time()
        print('epoch {}, total time {:.2f}'.format(epoch, time2 - time1))
        
        if  epoch != 0 and epoch % opt.save_freq == 0 or epoch == opt.epochs:
            print('==> Saving...')
            save_file = os.path.join(opt.checkpoint_path, 'ckpt_epoch{epoch}_lr{lr}_h{H}_K{k}_L{L}_b{beta}_nl{nl}_t{t}_samp{samp}.pth'.format(epoch=epoch,
                                                                                                                                              lr=opt.learning_rate,
                                                                                                                                              H=opt.hidden_dim,k=opt.numk,
                                                                                                                                              L=opt.L,beta=opt.beta, nl=opt.n_layers,
                                                                                                                                              t=opt.temp_s, samp=opt.samp))
                
            torch.save(model.query_model.state_dict(), save_file)
        # saving the model
        state = {
                'opt': opt,
                'state_dict': model.state_dict(),
                'optimizer': optimizer.state_dict(),
                'epoch': epoch,
            }

        save_file = os.path.join(opt.checkpoint_path, 'ckpt_epoch{epoch}_h2d_gnn.pth'.format(epoch=epoch))
        torch.save(state, save_file)

        # help release GPU memory
        del state
        torch.cuda.empty_cache()
    
    print('The trained alpha value = {}'.format(model.alpha))
    if opt.save_fig:
        fig, ax = plt.subplots(1, 1, figsize=(9, 6), tight_layout=True)
        ax.plot(np.arange(epoch+1),np.array(loss_epochs))
        ax.set_xlabel('Epochs')
        ax.set_ylabel('KL-divergence Loss')
        save_path = os.path.join(opt.checkpoint_path, 'loss_epoch{epoch}_lr{lr}_h{H}_K{k}_L{L}_b{beta}_nl{nl}_t{t}_samp{samp}.png'.format(epoch=epoch,
                                                                                                                                              lr=opt.learning_rate,
                                                                                                                                              H=opt.hidden_dim,k=opt.numk,
                                                                                                                                              L=opt.L,beta=opt.beta, nl=opt.n_layers,
                                                                                                                                              t=opt.temp_s,samp=opt.samp))
        fig.savefig(save_path, dpi=250)
        plt.close()    
    
main(opt)   
    
    
    