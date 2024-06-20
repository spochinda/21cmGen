#import contextlib
import contextlib
import torch
import socket
from datetime import datetime, timedelta
"""
def trace_handler(prof: torch.profiler.profile):
   # Prefix for file names.
   host_name = socket.gethostname()
   timestamp = datetime.now().strftime(TIME_FORMAT_STR)
   file_prefix = f"{host_name}_{timestamp}"

   # Construct the trace file.
   prof.export_chrome_trace(f"{file_prefix}.json.gz")

   # Construct the memory timeline file.
   prof.export_memory_timeline(f"{file_prefix}.html", device="cuda:0")

with torch.profiler.profile(
        activities=[
            torch.profiler.ProfilerActivity.CPU,
            torch.profiler.ProfilerActivity.CUDA,
            ],
        schedule=torch.profiler.schedule(wait=0, warmup=0, active=6, repeat=1),
        record_shapes=True,
        profile_memory=True,
        on_trace_ready=trace_handler,
        with_stack=True,
        ) as prof:
    with torch.autograd.profiler.record_function("## data prep in loop ##"):                
        torch.nn.Upsample(scale_factor=4, mode='trilinear')(torch.randn(1,1,64,64,64))


prof.export_memory_timeline("memory_trace.html")
"""
import torch.distributed
import torch.utils
from utils import *
from diffusion import *
from model import *
from model_edm import SongUNet
from loss import *
from sde_lib import VPSDE
import torch 
import torch.nn as nn

import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec as GS, GridSpecFromSubplotSpec as SGS

import torch.multiprocessing as mp
import torch.utils
from torch.utils.data.distributed import DistributedSampler
#from torch.nn.parallel import DistributedDataParallel as DDP
from torch.distributed import init_process_group, destroy_process_group

import time
import sys
import os



#from kymatio.scattering3d.backend.torch_backend import TorchBackend3D
#from kymatio.scattering3d.backend.torch_skcuda_backend import TorchSkcudaBackend3D
#from kymatio.torch import HarmonicScattering3D

#from torch_ema import ExponentialMovingAverage


def ddp_setup(rank: int, world_size: int):
    try:
        os.environ["MASTER_ADDR"] #check if master address exists
        print("Found master address: ", os.environ["MASTER_ADDR"])
    except:
        print("Did not find master address variable. Setting manually...")
        os.environ["MASTER_ADDR"] = "localhost"

    
    os.environ["MASTER_PORT"] = "2595"#"12355" 
    torch.cuda.set_device(rank)
    init_process_group(backend="nccl", rank=rank, world_size=world_size) #backend gloo for cpus?

def trace_handler(prof: torch.profiler.profile):
   # Prefix for file names.
   host_name = socket.gethostname()
   #timestamp = datetime.now().strftime(TIME_FORMAT_STR)
   file_prefix = f"{host_name}_"#{timestamp}"
   print("inside trace_handler, rank: ", torch.distributed.get_rank(), flush=True)
   # Construct the trace file.
   #prof.export_chrome_trace(f"{file_prefix}.json.gz")

   # Construct the memory timeline file.
   prof.export_memory_timeline(f"{file_prefix}.html", device="cuda:0")





def train_step(netG, epoch, train_dataloader, volume_reduction = False, split_batch=True, device="cpu", multi_gpu = False,):
    """
    Train the model
    """
    netG.model.train()
    
    avg_loss = torch.tensor(0.0, device=device)

    if multi_gpu:
        train_dataloader.sampler.set_epoch(epoch) #fix for ddp loaded checkpoint?

    for i,(T21, delta, vbv, T21_lr, labels) in enumerate(train_dataloader):

        if volume_reduction:
            if netG.network_opt["label_dim"] > 0:
                cut_factor = torch.randint(low=1, high=3, size=(1,), device=device)#.item()
            else:
                cut_factor = torch.randint(low=2, high=3, size=(1,), device=device)#always cut 2
                #cut_factor = torch.randint(low=1, high=2, size=(1,), device=device)#always cut 1

            T21 = get_subcubes(cubes=T21, cut_factor=cut_factor)
            delta = get_subcubes(cubes=delta, cut_factor=cut_factor)
            vbv = get_subcubes(cubes=vbv, cut_factor=cut_factor)
            T21_lr = get_subcubes(cubes=T21_lr, cut_factor=cut_factor)
                        
            volume_frac = 1/(2**cut_factor)**3 #* torch.ones(size=(T21.shape[0],), device=device)
            multiple_redshifts = False
            labels = volume_frac #torch.tensor([volume_frac,], device=device) #torch.cat([labels, volume_frac], dim=1) if multiple_redshifts else

            

        if netG.network_opt["label_dim"] > 0:
            labels = labels
        else:
            labels = None
        
        #with torch.no_grad():
        #T21_lr_min = torch.amin(T21_lr, dim=(1,2,3,4), keepdim=True)
        #T21_lr_max = torch.amax(T21_lr, dim=(1,2,3,4), keepdim=True)
        T21_lr_mean = torch.mean(T21_lr, dim=(1,2,3,4), keepdim=True)
        T21_lr_std = torch.std(T21_lr, dim=(1,2,3,4), keepdim=True)
        T21_lr = torch.nn.Upsample(scale_factor=4, mode='trilinear')(T21_lr)
        
        T21_lr, T21_lr_stats = normalize(T21_lr, mode="standard")
        T21, T21_stats = normalize(T21, mode="standard", x_mean=T21_lr_mean, x_std=T21_lr_std)
        delta, delta_extrema = normalize(delta, mode="standard")
        vbv, vbv_extrema = normalize(vbv, mode="standard")
        T21, delta, vbv , T21_lr = augment_dataset(T21, delta, vbv, T21_lr, n=1) #support device
        if split_batch: #split subcube minibatch into smaller mini-batches for memory
            sub_data = torch.utils.data.TensorDataset(T21, delta, vbv, T21_lr)
            sub_dataloader = torch.utils.data.DataLoader(sub_data, batch_size=4, shuffle=False, sampler = None) # (2**(cut_factor.item()-1))**3 // 2 #4
            if False:#str(device)=="cuda:0":
                print("Splitting batch...", flush=True)            
            for j,(T21, delta, vbv, T21_lr) in enumerate(sub_dataloader):

                #if str(device)=="cuda:0":
                #    print(f"Shape of T21: {T21.shape}", flush=True)
                
                netG.optG.zero_grad()
                loss = netG.loss_fn(net=netG, images=T21, conditionals=[delta, vbv, T21_lr],
                                    labels=labels, augment_pipe=None,
                                    )
                avg_loss = avg_loss + loss * T21.shape[0]  #add avg loss per mini-batch to accumulate total batch loss
                #with torch.autograd.profiler.record_function("## backward ##") if False else contextlib.nullcontext(): 
                loss.backward()
                #with torch.autograd.profiler.record_function("## optimizer ##") if False else contextlib.nullcontext():            
                torch.nn.utils.clip_grad_norm_(netG.model.parameters(), 1.0)
                netG.optG.step()        
                netG.ema.update() #Update netG.model with exponential moving average
                
                #with torch.autograd.profiler.record_function("## ema ##") if False else contextlib.nullcontext():                
                
        else:
            loss = netG.loss_fn(net=netG, images=T21, conditionals=[delta, vbv, T21_lr],
                                    labels=labels, augment_pipe=None,
                                    )
            avg_loss = avg_loss + loss * T21.shape[0]  #add avg loss per mini-batch to accumulate total batch loss
            #with torch.autograd.profiler.record_function("## backward ##") if False else contextlib.nullcontext(): 
            loss.backward()
            #with torch.autograd.profiler.record_function("## optimizer ##") if False else contextlib.nullcontext():            
            torch.nn.utils.clip_grad_norm_(netG.model.parameters(), 1.0)
            netG.optG.zero_grad()
            netG.optG.step()        
            #with torch.autograd.profiler.record_function("## ema ##") if False else contextlib.nullcontext():                
            netG.ema.update() #Update netG.model with exponential moving average

        
        if (str(device)=="cuda:0") or (str(device)=="cpu"):
            if False: #i%(len(train_data)//16) == 0:
                print(f"Batch {i} of {len(train_data)} batches")

        break #only do one box for now
    
    if multi_gpu:
        torch.distributed.all_reduce(tensor=avg_loss, op=torch.distributed.ReduceOp.SUM) #total loss=sum(average total batch loss per gpu)

    netG.loss.append(avg_loss.item())
    
    return avg_loss.item()

@torch.no_grad()
def plot_checkpoint(T21, T21_lr, delta, vbv, T21_pred, MSE=None, epoch=None, path = None, device="cpu"):
    model_idx = 0

    k_vals_true, dsq_true  = calculate_power_spectrum(T21, Lpix=3, kbins=100, dsq = True, method="torch", device=device)
    k_vals_pred, dsq_pred  = calculate_power_spectrum(T21_pred, Lpix=3, kbins=100, dsq = True, method="torch", device=device)
    
    #detatch and send to cpu
    T21 = T21.detach().cpu()
    T21_lr = T21_lr.detach().cpu()
    delta = delta.detach().cpu()
    vbv = vbv.detach().cpu()
    T21_pred = T21_pred.detach().cpu()
    
    k_vals_true = k_vals_true.detach().cpu()
    dsq_true = dsq_true.detach().cpu()
    k_vals_pred = k_vals_pred.detach().cpu()
    dsq_pred = dsq_pred.detach().cpu()

    
    slice_idx = T21.shape[-3]//2

    fig = plt.figure(figsize=(15,15))
    gs = GS(3, 3, figure=fig,) #height_ratios=[1,1,1.5])


    ax_delta = fig.add_subplot(gs[0,0])#, wspace = 0.2)
    ax_vbv = fig.add_subplot(gs[0,1])
    ax_T21_lr = fig.add_subplot(gs[0,2])

    ax_delta.imshow(delta[model_idx,0,slice_idx], vmin=-1, vmax=1)
    ax_delta.set_title("Delta (input)")
    ax_vbv.imshow(vbv[model_idx,0,slice_idx], vmin=-1, vmax=1)
    ax_vbv.set_title("Vbv (input)")
    ax_T21_lr.imshow(T21_lr[model_idx,0,slice_idx],)
    ax_T21_lr.set_title("T21 LR (input)")


    ax_T21 = fig.add_subplot(gs[1,0])
    ax_T21_pred = fig.add_subplot(gs[1,1])

    vmin = torch.amin(T21[model_idx,0,slice_idx]).item()
    vmax = torch.amax(T21[model_idx,0,slice_idx]).item()
    ax_T21.imshow(T21[model_idx,0,slice_idx], vmin=vmin, vmax=vmax)
    ax_T21.set_title("T21 HR (Real)")
    
    ax_T21_pred.imshow(T21_pred[model_idx,0,slice_idx], vmin=vmin, vmax=vmax)
    ax_T21_pred.set_title(f"T21 SR (Generated) epoch {epoch}")


    ax_hist = fig.add_subplot(gs[2,0])
    ax_hist.hist(T21_pred[model_idx,0,:,:,:].flatten(), bins=100, alpha=0.5, label="T21 SR", density=True)
    ax_hist.hist(T21[model_idx,0,:,:,:].flatten(), bins=100, alpha=0.5, label="T21 HR", density=True)
    ax_hist.set_xlabel("Norm. $T_{{21}}$")
    ax_hist.set_ylabel("PDF")
    ax_hist.legend()
    ax_hist.set_title(f"Sample MSE: {MSE:.4f}")

    ax_dsq = fig.add_subplot(gs[2,1])
    ax_dsq.plot(k_vals_true, dsq_true[model_idx,0], label="T21 HR", ls='solid', lw=2)
    ax_dsq.plot(k_vals_pred, dsq_pred[model_idx,0], label="T21 SR", ls='solid', lw=2)
    ax_dsq.set_ylabel('$\Delta^2(k)_\\mathrm{{norm}}$')
    ax_dsq.set_xlabel('$k$')
    ax_dsq.set_yscale('log')
    ax_dsq.grid()
    ax_dsq.legend()



    if False:
        sgs = SGS(1,2, gs[2,:])
        sgs_dsq = SGS(2,1, sgs[0], height_ratios=[4,1], hspace=0, )
        ax_dsq = fig.add_subplot(sgs_dsq[0])
        ax_dsq.get_xaxis().set_visible(False)
        ax_dsq_resid = fig.add_subplot(sgs_dsq[1], sharex=ax_dsq)
        ax_dsq_resid.set_ylabel("|Residuals|")#("$\Delta^2(k)_\\mathrm{{SR}} - \Delta^2(k)_\\mathrm{{HR}}$")
        ax_dsq_resid.set_xlabel("$k$")
        ax_dsq_resid.set_yscale('log')
        ax_dsq_resid.grid()
        
        #ax_dsq.plot(k_vals_true, dsq_pred[:,0].T, alpha=0.02, color='k', ls='solid')
        ax_dsq.plot(k_vals_true, dsq_true[model_idx,0], label="T21 HR", ls='solid', lw=2)
        ax_dsq.plot(k_vals_pred, dsq_pred[model_idx,0], label="T21 SR", ls='solid', lw=2)

        ax_dsq_resid.plot(k_vals_true, torch.abs(dsq_pred[:,0] - dsq_true[:,0]).T, color='k', alpha=0.02)
        ax_dsq_resid.plot(k_vals_true, torch.abs(dsq_pred[model_idx,0] - dsq_true[model_idx,0]), lw=2, )

        
        ax_dsq.set_ylabel('$\Delta^2(k)_\\mathrm{{norm}}$')
        #ax_dsq.set_xlabel('$k$')
        ax_dsq.set_yscale('log')
        ax_dsq.grid()
        ax_dsq.legend()
        ax_dsq.set_title("Power Spectrum (output)")


        ax_hist = fig.add_subplot(sgs[1])
        ax_hist.hist(x_pred[model_idx,0,:,:,:].flatten(), bins=100, alpha=0.5, label="T21 SR", density=True)
        ax_hist.hist(x_true[model_idx,0,:,:,:].flatten(), bins=100, alpha=0.5, label="T21 HR", density=True)
        
        ax_hist.set_xlabel("Norm. $T_{{21}}$")
        ax_hist.set_ylabel("PDF")
        ax_hist.legend()
        ax_hist.grid()
        ax_hist.set_title("Pixel Histogram (output)")

    plt.savefig(path)
    plt.close()

@torch.no_grad()
def validation_step_v2(netG, validation_dataloader, split_batch = True, device="cpu", multi_gpu=False):
    assert netG.noise_schedule_opt["schedule_type"] == "VPSDE", "Only VPSDE sampler supported for validation_step_v2"

    netG.model.eval()
    for i,(T21, delta, vbv, T21_lr, labels) in tqdm(enumerate(validation_dataloader), desc='validation loop', total=len(validation_dataloader), disable=False if str(device)=="cuda:0" else True):
        # Rest of the code
        # alternating cut_factor 1 and 2:
        #cut_factor = torch.tensor([i%2 + 1], device=device) #randomness comes from validation_dataloader shuffle so it is not the same cubes cut each time
        cut_factor = torch.tensor([1], device=device) #randomness comes from validation_dataloader shuffle so it is not the same cubes cut each time
        
        T21 = get_subcubes(cubes=T21, cut_factor=cut_factor)
        delta = get_subcubes(cubes=delta, cut_factor=cut_factor)
        vbv = get_subcubes(cubes=vbv, cut_factor=cut_factor)
        T21_lr = get_subcubes(cubes=T21_lr, cut_factor=cut_factor)
                    
        volume_frac = 1/(2**cut_factor)**3 #* torch.ones(size=(T21.shape[0],), device=device)
        multiple_redshifts = False
        labels = volume_frac #torch.tensor([volume_frac,], device=device) #torch.cat([labels, volume_frac], dim=1) if multiple_redshifts else

        if netG.network_opt["label_dim"] > 0:
            labels = labels
        else:
            labels = None
        
        #T21_lr_min = torch.amin(T21_lr, dim=(1,2,3,4), keepdim=True)
        #T21_lr_max = torch.amax(T21_lr, dim=(1,2,3,4), keepdim=True)
        T21_lr_mean = torch.mean(T21_lr, dim=(1,2,3,4), keepdim=True)
        T21_lr_std = torch.std(T21_lr, dim=(1,2,3,4), keepdim=True)
        T21_lr = torch.nn.Upsample(scale_factor=4, mode='trilinear')(T21_lr)
        
        T21_lr, T21_lr_extrema = normalize(T21_lr, mode="standard")
        T21, T21_extrema = normalize(T21, mode="standard", x_mean=T21_lr_mean, x_std=T21_lr_std)
        delta, delta_extrema = normalize(delta, mode="standard")
        vbv, vbv_extrema = normalize(vbv, mode="standard")
        T21, delta, vbv , T21_lr = augment_dataset(T21, delta, vbv, T21_lr, n=1) #support device
    

        if split_batch: #split subcube minibatch into smaller mini-batches for memory
            sub_data = torch.utils.data.TensorDataset(T21, delta, vbv, T21_lr)
            sub_dataloader = torch.utils.data.DataLoader(sub_data, batch_size=(2**(cut_factor.item()-1))**3, shuffle=False, sampler = None) 
            
            for j,(T21, delta, vbv, T21_lr) in tqdm(enumerate(sub_dataloader), desc='validation loop', total=len(sub_dataloader), disable=True):
                T21_pred = netG.sample.Euler_Maruyama_sampler(netG=netG, x_lr=T21_lr, conditionals=[delta, vbv], class_labels=labels, num_steps=100, eps=1e-3, clip_denoised=False, verbose=False)
                MSE_j = torch.mean(torch.square(T21_pred[:,-1:] - T21),dim=(1,2,3,4), keepdim=False)
                if j == 0:
                    MSE_i = MSE_j
                else:
                    MSE_i = torch.cat([MSE_i, MSE_j], dim=0)
                if i == 0:
                    break #only do one subbatch for now
        
        else:
            T21_pred = netG.sample.Euler_Maruyama_sampler(netG=netG, x_lr=T21_lr, conditionals=[delta, vbv], class_labels=labels, num_steps=100, eps=1e-3, clip_denoised=False, verbose=False)
            MSE_i = torch.mean(torch.square(T21_pred[:,-1:] - T21),dim=(1,2,3,4), keepdim=False)
        
        if i == 0:
            MSE = MSE_i
        else:
            MSE = torch.cat([MSE, MSE_i], dim=0)

        break #only do one box for now
    
    if multi_gpu:
        MSE_tensor_list = [torch.zeros_like(MSE) for _ in range(torch.distributed.get_world_size())]
        torch.distributed.all_gather(tensor_list=MSE_tensor_list, tensor=MSE)
        MSE = torch.cat(MSE_tensor_list, dim=0)
    MSE = torch.mean(MSE).item()

    if str(device)=="cuda:0":
        path = os.getcwd().split("/21cmGen")[0] + "/21cmGen/plots/validation_plot_test.png"
        plot_checkpoint(T21=T21, T21_lr=T21_lr, delta=delta, vbv=vbv, T21_pred=T21_pred[:,-1:], MSE=MSE, epoch=len(netG.loss), path = path, device=device)

    return MSE


###START main pytorch multi-gpu tutorial###
def main(rank, world_size=0, total_epochs = 1, batch_size = 4, memory_profiling=False, model_id=21):
    multi_gpu = world_size > 1

    if multi_gpu:
        device = torch.device(f'cuda:{rank}')
        print("Multi GPU: {0}, device: {1}".format(multi_gpu,device))
        ddp_setup(rank, world_size=world_size)


        
    else:
        device = "cpu"
        print("Multi GPU: {0}, device: {1}".format(multi_gpu,device))
    


    #optimizer and model
    path = os.getcwd().split("/21cmGen")[0] + "/21cmGen"

    with torch.autograd.profiler.record_function("## setup load data ##") if False else contextlib.nullcontext():
        #network_opt = dict(in_channel=4, out_channel=1, inner_channel=32, norm_groups=8, channel_mults=(1, 2, 4, 8, 8), attn_res=(16,8,), res_blocks=2, dropout = 0, with_attn=True, image_size=64, dim=3)
        #network_opt = dict(in_channel=4, out_channel=1, inner_channel=32, norm_groups=8, channel_mults=(1, 2, 4, 8, 8), attn_res=(8,), res_blocks=2, dropout = 0, with_attn=True, image_size=32, dim=3)
        #network = UNet
        network_opt = dict(img_resolution=64, in_channels=4, out_channels=1, label_dim=0, # (for tokens?), augment_dim,
                        model_channels=32, channel_mult=[2,2,2], attn_resolutions=[], #channel_mult_emb, num_blocks, attn_resolutions, dropout, label_dropout,
                        embedding_type='positional', channel_mult_noise=1, encoder_type='standard', decoder_type='standard', resample_filter=[1,1], 
                        )
        
        #network = UNet
        network = SongUNet
        
        #noise_schedule_opt = {'schedule_type': "linear", 'schedule_opt': {"timesteps": 1000, "beta_start": 0.0001, "beta_end": 0.02}} 
        #noise_schedule_opt = {'schedule_type': "cosine", 'schedule_opt': {"timesteps": 1000, "s" : 0.008}} 
        #noise_schedule_opt = {'schedule_type': "VPSDE", 'schedule_opt': {"timesteps": 1000, "beta_min" : 0.1, "beta_max": 20.0}}  
        noise_schedule_opt = {'schedule_type': "VPSDE", 'schedule_opt': {"timesteps": 1000, "beta_min" : 0.1, "beta_max": 20.0}}  
        
        #loss_fn = EDMLoss(P_mean=-1.2, P_std=1.2, sigma_data=0.5)
        loss_fn = VPLoss(beta_max=20., beta_min=0.1, epsilon_t=1e-5)
        
        netG = GaussianDiffusion(
                network=network,
                network_opt=network_opt,
                noise_schedule_opt=noise_schedule_opt,
                loss_fn = loss_fn,
                learning_rate=1e-4,
                scheduler=True,
                rank=rank,
            )
        
    #    SDE = VPSDE(beta_min=0.1, beta_max=20, N=1000)

        try:
            fn = path + "/trained_models/model_11/DDPMpp_lr_standard_labeldim_{0}_64_{1}_{2}".format(netG.network_opt["label_dim"], netG.noise_schedule_opt["schedule_type"], model_id)
            netG.load_network(fn+".pth")
            print("Loaded network at {0}".format(fn), flush=True)
        except Exception as e:
            print(e, flush=True)
            print("Failed to load network at {0}. Starting from scratch.".format(fn+".pth"), flush=True)

        train_data_module = CustomDataset(path_T21=path+"/outputs/T21_cubes_256/", path_IC=path+"/outputs/IC_cubes_256/", 
                                        redshifts=[10,], IC_seeds=list(range(0,56)), upscale=4, cut_factor=0, transform=False, norm_lr=True, device=device)
        #train_data_module = CustomDataset(path_T21=path+"/outputs/T21_cubes_128/", path_IC=path+"/outputs/IC_cubes_128/", 
        #                                redshifts=[10,], IC_seeds=list(range(1000,1008)), upscale=4, cut_factor=0, transform=False, norm_lr=True, device=device)
        #train_dataset, train_dataset_norm, train_dataset_extrema = train_data_module.getFullDataset()

        #train_dataloader = torch.utils.data.DataLoader( train_dataset, batch_size=batch_size, shuffle=False if multi_gpu else True, 
        #                                        sampler = DistributedSampler(train_dataset) if multi_gpu else None) #4
        #train_data_norm = torch.utils.data.DataLoader( train_dataset_norm, batch_size=batch_size, shuffle=False if multi_gpu else True, 
        #                                         sampler = DistributedSampler(train_dataset_norm) if multi_gpu else None) #4
        train_dataloader = torch.utils.data.DataLoader(train_data_module, batch_size=batch_size, shuffle=False if multi_gpu else True,
                                                        sampler = DistributedSampler(train_data_module) if multi_gpu else None)
        
        
        validation_data_module = CustomDataset(path_T21=path+"/outputs/T21_cubes_256/", path_IC=path+"/outputs/IC_cubes_256/", 
                                        redshifts=[10,], IC_seeds=list(range(56,72)), upscale=4, cut_factor=0, transform=False, norm_lr=True, device=device)
        validation_dataloader = torch.utils.data.DataLoader(validation_data_module, batch_size=batch_size, shuffle=False if multi_gpu else True,
                                                        sampler = DistributedSampler(validation_data_module) if multi_gpu else None)
        
        if False: #72-80 extra validation
            validation_data_module = CustomDataset(path_T21=path+"/outputs/T21_cubes_256/", path_IC=path+"/outputs/IC_cubes_256/", 
                                                redshifts=[10,], IC_seeds=list(range(56,72)), upscale=4, cut_factor=1, transform=False, norm_lr=True, device=device)
            validation_data_module_small = CustomDataset(path_T21=path+"/outputs/T21_cubes_256/", path_IC=path+"/outputs/IC_cubes_256/", 
                                                        redshifts=[10,], IC_seeds=list(range(56,80)), upscale=4, cut_factor=2, transform=False, norm_lr=True, device=device)

        
            validation_dataset, validation_dataset_norm, validation_dataset_extrema = validation_data_module.getFullDataset()
            validation_dataset_small, validation_dataset_norm_small, validation_dataset_extrema_small = validation_data_module_small.getFullDataset()
            validation_batch_size = 1
            validation_batch_size_small = validation_dataset_small.tensors[0].shape[0]//validation_dataset.tensors[0].shape[0]
            print("Validation batch size: ", validation_batch_size, validation_batch_size_small, flush=True)
            validation_data_norm = torch.utils.data.DataLoader( validation_dataset_norm, batch_size=validation_batch_size, shuffle=False if multi_gpu else True, 
                                                        sampler = DistributedSampler(validation_dataset_norm) if multi_gpu else None) #4
            validation_data_norm_small = torch.utils.data.DataLoader( validation_dataset_norm_small, batch_size=validation_batch_size_small, shuffle=False if multi_gpu else True,
                                                            sampler = DistributedSampler(validation_dataset_norm_small) if multi_gpu else None) #4
    

    

    if (str(device)=="cuda:0") or (str(device)=="cpu"):
        
        print(f"[{device}] (Mini)Batchsize: {train_dataloader.batch_size} | Steps (batches): {len(train_dataloader)}", flush=True)
    
    if (str(device)=="cuda:0") and memory_profiling:
        torch.cuda.memory._record_memory_history()
        #prof.step()
        
    for e in range(total_epochs):        
        
        if (str(device)=="cuda:0") or (str(device)=="cpu"):
            start_time = time.time()
        
        
        avg_loss = train_step(netG=netG, epoch=e, train_dataloader=train_dataloader, volume_reduction=True, split_batch=True, device=device, multi_gpu=multi_gpu)


        if (str(device)=="cuda:0") or (str(device)=="cpu"):
            print("[{0}]: Epoch {1} in {2:.2f}s | loss: {3:.3f}, mean(loss[-10:]): {4:.3f}, loss min: {5:.3f}, learning rate: {6:.3e}".format(str(device), len(netG.loss), time.time()-start_time, 
                                                                                                                                              avg_loss,  torch.mean(torch.tensor(netG.loss[-10:])).item(), 
                                                                                                                                              torch.min(torch.tensor(netG.loss)).item(), netG.optG.param_groups[0]['lr']), flush=True)

            #if e<4 and multi_gpu: #memory snapshot
            #    try:
            #        torch.cuda.memory._dump_snapshot(f"memory_snapshot_{str(device)[-1]}.pickle")
            #    except Exception as E:
            #        print(f"Failed to capture memory snapshot {E}", flush=True)
            #elif e==4 and multi_gpu:
            #    torch.cuda.memory._record_memory_history(enabled=None)
                
        loss_min = torch.min( torch.tensor(netG.loss_validation["loss"]) ).item()

        

        if False:#avg_loss <= loss_min and e >= 4000:
            if rank ==0:
                print(f"[{device}] loss={avg_loss:.2f} smaller than saved loss={loss_min:.2f}, epoch {e}: validating...", flush=True)
            loss_validation = validation_step_v2(netG=netG, validation_data_norm=validation_data_norm, validation_data_norm_small=validation_data_norm_small, device=device, multi_gpu=multi_gpu)
            
            loss_validation_min = torch.min( torch.tensor(netG.loss_validation["loss_validation"]) ).item()
            if loss_validation < loss_validation_min:
                if rank==0:
                    print(f"[{device}] validation loss={loss_validation:.2f} smaller than validation minimum={loss_validation_min:.2f}", flush=True)
                #netG.save_network(fn+".pth")
                netG.loss_validation["loss"].append(avg_loss)
                netG.loss_validation["loss_validation"].append(loss_validation)
            else:
                if rank==0:
                    print(f"[{device}] Not saving... validation loss={loss_validation:.2f} larger than validation minimum={loss_validation_min:.2f}", flush=True)

        if False:#len(netG.loss)>=150:
            if (avg_loss == torch.min(torch.tensor(netG.loss)).item()) or len(netG.loss)%20==0:
                netG.save_network( fn+".pth" )
                #losses_validation, x_pred, k_and_dsq_and_idx = validation_step(netG=netG, validation_data=validation_data, validation_loss_type="dsq_voxel", device=device, multi_gpu=multi_gpu)
                
                if False: #(str(device)=="cuda:0") or (str(device)=="cpu"):
                    print("losses_validation: {0}, loss_validation minimum: {1}".format(losses_validation.item(), torch.min(torch.tensor(netG.loss_validation)).item()), flush=True)
                    
                    if losses_validation.item() == torch.min(torch.tensor(netG.loss_validation)).item():       
                        plot_checkpoint(validation_data, x_pred, k_and_dsq_and_idx=k_and_dsq_and_idx, epoch = e, path = fn+".png", device="cpu")
                        netG.save_network( fn+".pth" )
                    else:
                        print("Not saving model. Validaiton did not improve", flush=True)
        
        if len(netG.loss)>=30:
            if len(netG.loss)%8==0 or avg_loss == torch.min(torch.tensor(netG.loss)).item():
                with netG.ema.average_parameters():
                    loss_validation = validation_step_v2(netG=netG, validation_dataloader=validation_dataloader, split_batch = True, device=device, multi_gpu=multi_gpu)
                loss_validation_min = torch.min( torch.tensor(netG.loss_validation["loss_validation"]) ).item()
                if loss_validation < loss_validation_min:
                    if rank==0:
                        print(f"[{device}] validation loss={loss_validation:.2f} smaller than validation minimum={loss_validation_min:.2f}", flush=True)
                    netG.save_network(fn+".pth")
                    netG.loss_validation["loss_validation"].append(loss_validation)
                else:
                    if rank==0:
                        print(f"[{device}] Not saving... validation loss={loss_validation:.2f} larger than validation minimum={loss_validation_min:.2f}", flush=True)
            
        if netG.scheduler is not False:
            netG.scheduler.step()

        if (str(device)=="cuda:0") and memory_profiling:
            torch.cuda.memory._dump_snapshot(f"memory_snap_16_2_{str(device)[-1]}.pickle")
            #prof.step()

    
    if (str(device)=="cuda:0") and memory_profiling:
        torch.cuda.memory._record_memory_history(enabled=None)

    if multi_gpu:#world_size > 1:
        torch.distributed.barrier()
        destroy_process_group()
###END main pytorch multi-gpu tutorial###



if __name__ == "__main__":
    
    
    print("PyTorch version: ", torch.__version__)
    print("CUDA version: ", torch.version.cuda)
   
    world_size = torch.cuda.device_count()
    multi_gpu = world_size > 1

    if multi_gpu:
        print("Using multi_gpu", flush=True)
        for i in range(torch.cuda.device_count()):
            print("Device {0}: ".format(i), torch.cuda.get_device_properties(i).name)
        mp.spawn(main, args=(world_size, 100000, 1, False, 30), nprocs=world_size) #wordlsize, total_epochs, batch size (for minibatch)
    else:
        print("Not using multi_gpu",flush=True)
        try:
            main(rank=0, world_size=0, total_epochs=1, batch_size=1, memory_profiling=False, model_id=30)#2*4)
        except KeyboardInterrupt:
            print('Interrupted', flush=True)
            try:
                sys.exit(130)
            except SystemExit:
                os._exit(130)
    
        
