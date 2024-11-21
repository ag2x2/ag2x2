# Ag2x2

Code repository for **Ag2x2: A Robust Agent-Agnostic Visual Representation Boosts Zero-Shot Learning of Bimanual Robotic Manipulation**.

## Table of Contents
- [Environment Setup](#environment-setup)
- [Visual Representation](#visual-representation)
- [Bimanual Skills](#bimanual-skills)

## Environment Setup
1. Ensure you have the following software installed:
   - Python >= 3.6.8
   - Pytorch 1.13.1+cu117

2. Clone the project:
   ```bash
   git clone https://github.com/ag2x2/ag2X2.git
   cd ag2x2

3. Create conda environment:
   ```bash
   conda env create -f environment.yml

4. Install [IsaacGym](https://developer.nvidia.com/isaac-gym) following the official documentation.
   
## Visual Representation
1. Train our visual representation model on EPIC-KITCHEN dataset:
   - ``` bash
     cd repre_trainer
   - Run `train_ddp.py` to train our model on multiple GPU in parallel, or run `train.py` to train on a single GPU.
2. Modify `exp_name` in `repre_trainer/cfgs/scratch.yml` to save the model to the location you specify.
3. You can download our checkpoint [here](https://1drv.ms/u/s!AtoAqxZ1DxQscLqjqks969dqUcY?e=nLJFe2).

## Bimanual Skills
1. Change `ckpt_dir` according to the location you store your visual representation checkpoint.
2. Train bimanual tasks in IsaacGym with the following command:
   ```bash
   python train.py --task=ag2x2@close_door_outward@ag2x2 --algo=ppo --seed=42 --cfg_train=cfgs/algo/ppo/manipulation.yaml --disable_wandb --camera=default
   ```
   The best policy will be saved as `model_best.pt` in `logs/ag2x2/close_door_outward@default/ag2x2@ppo.42/`.
3. Inference and save a trajectory using the trained policy:
   ```bash
   python train.py --task=ag2x2@close_door_outward@ag2x2 --model_dir=logs/ag2x2/close_door_outward@default/ag2x2@ppo.42/model_best.pt --test --save_traj --algo=ppo --cfg_train=cfgs/algo/ppo/manipulation.yaml --camera=default --seed=0 --disable_wandb
   ```
   The trajectory will be saved as `logs/ag2x2/close_door_outward@default/ag2x2@ppo.42/absres_best.pkl`.
4. Plan with franka robot arms using the saved trajectory:
   ```bash
   python plan.py --task=ag2x2@close_door_outward@ag2x2 --traj_path=logs/ag2x2/close_door_outward@default/ag2x2@ppo.42/absres_best.pkl --pipeline=cpu --algo=ppo --cfg_train=cfgs/algo/ppo/manipulation.yaml --disable_wandb --camera=default
5. We test our model on 13 tasks, each with 9 runs (3 seeds x 3 camera positions). Check out our 13x9 experiment videos [here](https://1drv.ms/f/s!AtoAqxZ1DxQscVwnE4OF4ndbzTE?e=zg175H).
6. Additionally, please check out imitation learning training data and the inference video [here](https://1drv.ms/f/s!AtoAqxZ1DxQsggtbiVYByiexQj8p?e=XAaJnU).
