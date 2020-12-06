import multiprocessing
import operator
from functools import partial

import numpy as np

from core import mathlib
from core.interact import interact as io
from core.leras import nn
from facelib import FaceType, TernausNet
from models import ModelBase
from samplelib import *

class FANSegModel(ModelBase):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, force_model_class_name='FANSeg', **kwargs)
        
    #override
    def on_initialize_options(self):
        device_config = nn.getCurrentDeviceConfig()
        yn_str = {True:'y',False:'n'}

        ask_override = self.ask_override()
        if self.is_first_run() or ask_override:
            self.ask_autobackup_hour()
            self.ask_target_iter()
            self.ask_batch_size(24)

        default_lr_dropout         = self.options['lr_dropout']         = self.load_or_def_option('lr_dropout', False)
        
        if self.is_first_run() or ask_override:
            self.options['lr_dropout']  = io.input_bool ("Use learning rate dropout", default_lr_dropout, help_message="When the face is trained enough, you can enable this option to get extra sharpness and reduce subpixel shake for less amount of iterations.")
         
    #override
    def on_initialize(self):
        device_config = nn.getCurrentDeviceConfig()
        nn.initialize(data_format="NHWC")
        tf = nn.tf

        device_config = nn.getCurrentDeviceConfig()
        devices = device_config.devices

        self.resolution = resolution = 256
        self.face_type = FaceType.FULL
         
        place_model_on_cpu = len(devices) == 0
        models_opt_device = '/CPU:0' if place_model_on_cpu else '/GPU:0'

        bgr_shape = nn.get4Dshape(resolution,resolution,3)
        mask_shape = nn.get4Dshape(resolution,resolution,1)
 
        # Initializing model classes
        self.model = TernausNet(f'FANSeg_{FaceType.toString(self.face_type)}', 
                                 resolution, 
                                 load_weights=not self.is_first_run(),
                                 weights_file_root=self.get_model_root_path(),
                                 training=True,
                                 place_model_on_cpu=place_model_on_cpu,
                                 optimizer=nn.RMSprop(lr=0.0001, lr_dropout=0.3 if self.options['lr_dropout'] else 1.0,name='opt') )
                                 
        if self.is_training:
            # Adjust batch size for multiple GPU
            gpu_count = max(1, len(devices) )
            bs_per_gpu = max(1, self.get_batch_size() // gpu_count)
            self.set_batch_size( gpu_count*bs_per_gpu)


            # Compute losses per GPU
            gpu_pred_list = []

            gpu_losses = []
            gpu_loss_gvs = []
            
            for gpu_id in range(gpu_count):
                with tf.device( f'/GPU:{gpu_id}' if len(devices) != 0 else f'/CPU:0' ):

                    with tf.device(f'/CPU:0'):
                        # slice on CPU, otherwise all batch data will be transfered to GPU first
                        batch_slice = slice( gpu_id*bs_per_gpu, (gpu_id+1)*bs_per_gpu )
                        gpu_input_t       = self.model.input_t [batch_slice,:,:,:]
                        gpu_target_t      = self.model.target_t [batch_slice,:,:,:]                        
                        
                    # process model tensors
                    gpu_pred_logits_t, gpu_pred_t = self.model.net([gpu_input_t])                    
                    gpu_pred_list.append(gpu_pred_t)
 
                    gpu_loss = tf.reduce_mean( tf.nn.sigmoid_cross_entropy_with_logits(labels=gpu_target_t, logits=gpu_pred_logits_t), axis=[1,2,3])
                    gpu_losses += [gpu_loss]

                    gpu_loss_gvs += [ nn.gradients ( gpu_loss, self.model.net_weights ) ]


            # Average losses and gradients, and create optimizer update ops
            with tf.device (models_opt_device):
                pred = nn.concat(gpu_pred_list, 0)                
                loss = tf.reduce_mean(gpu_losses)
                
                loss_gv_op = self.model.opt.get_update_op (nn.average_gv_list (gpu_loss_gvs))
  
        
            # Initializing training and view functions
            def train(input_np, target_np):
                l, _ = nn.tf_sess.run ( [loss, loss_gv_op], feed_dict={self.model.input_t :input_np, self.model.target_t :target_np })
                return l
            self.train = train

            def view(input_np):
                return nn.tf_sess.run ( [pred], feed_dict={self.model.input_t :input_np})
            self.view = view

            # initializing sample generators
            training_data_src_path = self.training_data_src_path
            training_data_dst_path = self.training_data_dst_path

            cpu_count = min(multiprocessing.cpu_count(), 8)
            src_generators_count = cpu_count // 2
            dst_generators_count = cpu_count // 2
            src_generators_count = int(src_generators_count * 1.5)

            src_generator = SampleGeneratorFace(training_data_src_path, random_ct_samples_path=training_data_src_path, debug=self.is_debug(), batch_size=self.get_batch_size(),
                                                sample_process_options=SampleProcessor.Options(random_flip=True),
                                                output_sample_types = [ {'sample_type': SampleProcessor.SampleType.FACE_IMAGE,  'ct_mode':'lct', 'warp':True, 'transform':True, 'channel_type' : SampleProcessor.ChannelType.BGR,                                                            'face_type':self.face_type, 'random_motion_blur':(25, 5),  'random_gaussian_blur':(25,5), 'data_format':nn.data_format, 'resolution': resolution},
                                                                        {'sample_type': SampleProcessor.SampleType.FACE_MASK,                    'warp':True, 'transform':True, 'channel_type' : SampleProcessor.ChannelType.G,   'face_mask_type' : SampleProcessor.FaceMaskType.FULL_FACE, 'face_type':self.face_type,                                                               'data_format':nn.data_format, 'resolution': resolution},
                                                                        ],
                                                generators_count=src_generators_count )
                                                
            dst_generator = SampleGeneratorFace(training_data_dst_path, debug=self.is_debug(), batch_size=self.get_batch_size(),
                                                sample_process_options=SampleProcessor.Options(random_flip=True),
                                                output_sample_types = [ {'sample_type': SampleProcessor.SampleType.FACE_IMAGE,  'warp':False, 'transform':True, 'channel_type' : SampleProcessor.ChannelType.BGR, 'face_type':self.face_type, 'data_format':nn.data_format, 'resolution': resolution},
                                                                    ],
                                                generators_count=dst_generators_count,
                                                raise_on_no_data=False )
            if not dst_generator.is_initialized():
                io.log_info(f"\nTo view the model on unseen faces, place any aligned faces in {training_data_dst_path}.\n")
                
            self.set_training_data_generators ([src_generator, dst_generator])

    #override
    def get_model_filename_list(self):
        return self.model.model_filename_list

    #override
    def onSave(self):
        self.model.save_weights()
        
    #override
    def onTrainOneIter(self):        
        source_np, target_np = self.generate_next_samples()[0]
        loss = self.train (source_np, target_np)       

        return ( ('loss', loss ), )

    #override
    def onGetPreview(self, samples):
        n_samples = min(4, self.get_batch_size(), 800 // self.resolution )

        src_samples, dst_samples = samples        
        source_np, target_np = src_samples

        S, TM, SM, = [ np.clip(x, 0.0, 1.0) for x in ([source_np,target_np] + self.view (source_np) ) ]
        TM, SM, = [ np.repeat (x, (3,), -1) for x in [TM, SM] ]

        green_bg = np.tile( np.array([0,1,0], dtype=np.float32)[None,None,...], (self.resolution,self.resolution,1) )

        result = []        
        st = []
        for i in range(n_samples):
            ar = S[i]*TM[i] + 0.5*S[i]*(1-TM[i]) + 0.5*green_bg*(1-TM[i]), SM[i], S[i]*SM[i] + green_bg*(1-SM[i])
            st.append ( np.concatenate ( ar, axis=1) )
        result += [ ('FANSeg training faces', np.concatenate (st, axis=0 )), ]
        
        if len(dst_samples) != 0:
            dst_np, = dst_samples
            
            D, DM, = [ np.clip(x, 0.0, 1.0) for x in ([dst_np] + self.view (dst_np) ) ]
            DM, = [ np.repeat (x, (3,), -1) for x in [DM] ]
        
            st = []
            for i in range(n_samples):
                ar = D[i], DM[i], D[i]*DM[i]+ green_bg*(1-DM[i])
                st.append ( np.concatenate ( ar, axis=1) )
            
            result += [ ('FANSeg unseen faces', np.concatenate (st, axis=0 )), ]
            
        return result

Model = FANSegModel
