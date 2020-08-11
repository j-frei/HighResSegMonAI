# Segmentation with MonAI

## How to run training/evaluation inside Docker
   * Docker Parameter Info:  
   ```
   -p 6543:6006 # for tensorboard access at port 6543
   -v /data/path/to/volumes:/root/data:ro # for mounting image data (read only)
   -v /data/path/to/repo/dir:/root # for mounting code + write results
   --name=monai_segmentation # container name
   --gpus device=2 # use last gpu (gpu id 2) 
   ```

   * Start/Create Container:  
   ```bash
   docker run --gpus device=2 --rm -ti \
       -p 6543:6006 \
       -v /data/path/to/volumes:/root/data:ro \
       -v /data/path/to/repo/dir:/root \
       --ipc=host --name=monai_segmentation projectmonai/monai:latest
   ```

## How to train/eval/use tensorboard inside Docker
  * If errors occur: Reinstall pytorch to be compatible with older CUDA version 10.1:  
    `pip install torch==1.5.0+cu101 torchvision==0.6.0+cu101 -f https://download.pytorch.org/whl/torch_stable.html`

  * Train/Eval:
    Within container shell (after calling "docker run ..."), enter code directory by calling:  
    `cd /root`

  * After that, train the network:  
    `python3 train.py`  
    OR use nohup to let it run in the background (with &):  
    `nohup python3 train.py &`  
    and read the output by:  
    `tail +1f nohup.out`

  * Evaluate using test set:  
    `python3 eval.py`

## Using tensorboard
  * Run tensorboard and access it through http://HOSTNAME:6543/ :  
    `docker exec -ti monai_segmentation bash -c "tensorboard --logdir=/root/runs --bind_all"`
