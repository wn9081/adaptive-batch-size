import numpy as np
import math
import tensorflow as tf
from mlppolicy import NormalPolicy
import random

#OpenAI
import gym
from stock_simulator import TradingEnv

#parallelism
import sys
from joblib import Parallel, delayed
import multiprocessing
import tempfile, os

#meta
import time

#REINFORCE gradient estimator (w/o final averaging)
def reinforce(scores,disc_rewards):
    q = np.sum(disc_rewards,1)
    sum_of_scores = np.sum(scores,1)
    #optimal baseline:
    b = 0
    score_mean = np.mean(sum_of_scores**2)
    if score_mean!=0:
        b = np.mean(sum_of_scores**2*q)/score_mean
    #gradient estimates:
    return sum_of_scores*(q-b)

#GPOMDP gradient estimator (w/out final averaging)
def gpomdp(scores,disc_rewards):
    N = scores.shape[0]
    H = scores.shape[1]
    cumulative_scores = np.zeros((N,H))
    #optimal baseline:
    b = np.zeros(H)
    for k in range(0,H):
        cumulative_scores[:,k] = sum(scores[:,i] for i in range(0,k+1))
        cumul_score_mean = np.mean(cumulative_scores[:,k]**2)
        if cumul_score_mean!=0:
            b[k] = np.mean(cumulative_scores[:,k]**2*disc_rewards[:,k])/ \
                    cumul_score_mean 
    #gradient estimate:
    return sum(cumulative_scores[:,i]*(disc_rewards[:,i] - b[i]) for i in range(0,H))

#Generic closed form optimization for N and corresponding estimation error
def closed_opt(d,infgrad):
    eps_star = 0.25*(math.sqrt(17) - 3)*infgrad
    N_star = int(math.ceil(d**2/eps_star**2))
    return eps_star, N_star

#Optimization with Chebyshev bound for REINFORCE
def cheb_reinforce(R,M_phi,sigma,infgrad,sample_var=None,c=None,sample_rng=None):
    d =  math.sqrt((R**2*M_phi**2*H*(1-gamma**H)**2)/ \
                (sigma**2*(1-gamma)**2*delta))
    
    return (d,0) + closed_opt(d,infgrad)

#Optimization with Chebyshev bound for GPOMDP
def cheb_gpomdp(R,M_phi,sigma,infgrad,sample_var=None,c=None,sample_rng=None):
    d = math.sqrt((R**2*M_phi**2)/(delta*sigma**2*(1-gamma)**2) * \
                       ((1-gamma**(2*H))/(1-gamma**2)+ H*gamma**(2*H)  - \
                            2 * gamma**H  * (1-gamma**H)/(1-gamma)))
    return (d,0) + closed_opt(d,infgrad)

#Optimization with Hoeffding bound
def sample_hoeffding(R,M_phi,sigma,infgrad,sample_var,c,sample_rng):
    rng = sample_rng
    d = rng*math.sqrt(math.log(2/delta)/2)
    return (d,0) + closed_opt(d,infgrad)

#Optimization with empirical Bernstein bound
def sample_bernstein(R,M_phi,sigma,grads,sample_vars,c,sample_ranges):

    k = 0
    ups_max = -np.inf
    for j in range(m):
        if c[j]==0:
            continue
        d = math.sqrt(2*math.log(3.0/delta)*sample_vars[j])
        f = 3*sample_ranges[j]*math.log(3.0/delta)
        eps = d/math.sqrt(N_max) + f/N_max
        ups = (grads[j] - eps)**4/(4*c[j]*(grads[j] + eps)**2/N_max)
        if ups > ups_max:
            ups_max = ups
            k = j
    
    infgrad = grads[k]
    d = math.sqrt(2*math.log(3.0/delta)*sample_vars[k])
    f = 3*sample_ranges[k]*math.log(3.0/delta)
    N_0 = min(N_max,max(N_min,int(((d + math.sqrt(d**2 + 4*f*infgrad)) \
            /(2*infgrad))**2) + 1))
    ups_max = -np.inf
    eps_star = np.inf
    N_star = N_0
    print 'N0:', N_0
    improv = 0
    for N in range(N_0,N_max):
        eps = d/math.sqrt(N) + f/N
        upsilon = (infgrad - eps)**4/ \
                    (4*c[k]*(infgrad + eps)**2*N)
        if upsilon - ups_max > 0:
            improv = upsilon - ups_max
            ups_max = upsilon
            eps_star = eps
            N_star = N
        else: break
    print 'N*', N_star
    return d,f,eps_star,N_0#star


if __name__ == '__main__':
    env = gym.make('tradingsim-v0')

    #Fixed batch-size and step-size
    #N_fix = 100
    #alpha_fix = 1e-2

    #Task constants
    a_max = np.asscalar(env.action_space.high)
    a_min = np.asscalar(env.action_space.low)
    action_volume = a_max-a_min  #|A|
    gamma = 0.9
    H = env.days
    #Normal policy with 2-layers NN mean and fixed std
    obs_size = len(env.reset())
    n_obs = 1
    action_size = 1 #scalar action
    sigma = 0.1#1./math.sqrt(obs_size)
    hidden_neurons = []
    hidden_layers = 1
    state_var = tf.placeholder(tf.float32, [n_obs,obs_size])
    action_var = tf.placeholder(tf.float32, [n_obs,action_size])
    pol = NormalPolicy(1,hidden_neurons,[],a_min,a_max, \
                min_std=sigma,fixed_std=True)(state_var,action_var)
    
    print 'Trajectory size:', H
     
    #Options (args: delta, N_min, N_max, estimator,bound ,outfile, MaxN)
    verbose = 1 
    estimators = [reinforce,gpomdp]
    bounds = [cheb_reinforce,cheb_gpomdp,sample_hoeffding,sample_bernstein]
    delta = 0.99
    #if len(sys.argv)>1:
    #    delta = float(sys.argv[1])
    #assert delta<1
    N_min = 2
    #if len(sys.argv)>2:
    #    N_min = int(sys.argv[2])
    #assert N_min > 1
    N_max = 100000
    #if len(sys.argv)>3:
    #    N_max = int(sys.argv[3])
    #assert N_max < 1000000
    k = 1
    #if len(sys.argv)>4:
    #    k = int(sys.argv[4])
    #assert k<len(estimators)
    grad_estimator = estimators[k]
    k = 3
    #if len(sys.argv)>5:
    #    k = int(sys.argv[5])
    #assert k<len(bounds)
    stat_bound = bounds[k]
    print 'Using', grad_estimator.__name__, ',', stat_bound.__name__
    record = len(sys.argv) > 1
    if record:
        fp = open(sys.argv[1],'w')    
    N_maxtot = 1000000 * 10
    if len(sys.argv) > 2:
        N_maxtot = int(sys.argv[2])  

    def features(obs):
        s = obs
        for i in range(len(s)-1):
            s[i] = s[-1] - s[i]
        return s
 
    #Trajectory (to run in parallel)
    def trajectory(n,traces,max_f):#,noises):
        obs = env.reset()
        s = features(obs)
        s_feed = s.reshape((n_obs,obs_size))
        max_f[n,:m-1] = abs(s)
        max_f[n,m-1] = 1

        for l in range(H): 
            s_feed = s.reshape((n_obs,obs_size))
            mu = np.asscalar(pol.get_mu(s_feed))
            a = np.clip(mu + sigma*np.random.randn(),a_min,a_max)
            a_feed = a.reshape((n_obs,action_size))
            score = pol.log_gradients(s_feed,a_feed)
            traces[n,l,0:m] = score 
            obs,r,_,info = env.step(np.array([a]))
            s = features(obs)
            max_f[n,:m-1] = np.maximum(max_f[n,:m-1],abs(s))
            traces[n,l,m] = r  

        nav = info['nav']
        
        return nav

    #LEARNING

    if record:
        fp.write("{} {} {} {} {}\n\n".format(N_min,N_max,hidden_neurons,sigma,N_maxtot))

    path = tempfile.mkdtemp()
    #traces_path = os.path.join(path,'traces.mmap')
    grads_path = os.path.join(path,'grads.mmap')
    n_cores = multiprocessing.cpu_count() 
    
    with tf.Session() as sess:
        sess.run(tf.global_variables_initializer()) 
        #Initial policy parameter
        theta = pol.get_weights()
        m = len(theta)
        pol.reset(np.array([0.5]*obs_size+[0.5]))
        #pol.load_weights(idx=1110)

        N = N_min
        N_tot = N
        J_est = -np.inf
        bad_updates = 0
        R = 0
        M_phi = np.zeros(m)
        iteration = 0
        tot_deltaJ = 0
        gain = 0
        while True: 
            iteration+=1 
            if verbose > 0:
                start = time.time()
                print
                print 'iteration:', iteration, 'N:', N, #'theta:', theta  
                
            #Run N trajectories in parallel  
            #noises = np.random.normal(0,1,(N,H))
            #traces = np.memmap(traces_path,dtype=float,shape=(N,H,m+1),mode='w+')  
            #navs, max_features = zip(*Parallel(n_jobs=n_cores)(delayed(trajectory)(n,traces,noises[n]) for n in xrange(N)))
            traces = np.zeros((N,H,m+1))
            max_f = np.zeros((N,m))
            navs = [trajectory(n,traces,max_f) for n in range(N)]
            max_f = np.amax(max_f,0)
            scores = traces[:,:,0:m]
            rewards = traces[:,:,m]
            disc_rewards = rewards
            for n in range(N):
                for l in range(H):
                    disc_rewards[n,l]*=gamma**l

            #Performance estimation
            J_est0 = J_est
            J_est = np.mean(np.sum(disc_rewards,1))
            deltaJ_est = J_est - J_est0
            if iteration>1:
                if deltaJ_est<0:
                    bad_updates+=1
                eff = 1-float(bad_updates)/(iteration-1)
                tot_deltaJ+=deltaJ_est
                print 'EFF:', eff, '%' 
            nav = np.mean(np.array(navs))
            gain+=sum(np.array(navs)-1)
            if verbose>0:   
                print 'J~:', J_est
                print 'deltaJ~:', deltaJ_est
                print 'tot_deltaJ:', tot_deltaJ
                print 'nav(avg):', nav
                print 'gain:', gain
            del traces

            R = max(R,np.max(abs(rewards)))
            M_phi = np.maximum(M_phi,max_f)
            if verbose>0:
                print 'R:', R, 'M_phi:', M_phi
            c = [(R*M_phi[j]**2*(gamma*math.sqrt(2*math.pi)*sigma + 2*(1-gamma)*action_volume))/ \
                    (2*(1-gamma)**3*sigma**3*math.sqrt(2*math.pi)) for j in range(m)]
        
            #Gradient estimation
            print 'Computing gradients'
            grads_J = np.zeros(m)
            sample_vars = np.zeros(m)
        
            def compute_grads(j,grad_samples):
                grad_samples[j,:] = grad_estimator(scores[:,:,j],disc_rewards) 
        
            grad_samples = np.memmap(grads_path,dtype=float,shape=(m,N),mode='w+')  
            Parallel(n_jobs=n_cores,backend="threading")(delayed(compute_grads)(j,grad_samples) for j in xrange(m))
            for j in range(m):
                grads_J[j] = np.mean(grad_samples[j,:])
                sample_vars[j] = np.var(grad_samples[j],ddof=1)
            infgrad = max(abs(grads_J))
            print 'max_abs_grad:', infgrad
            k = np.argmax(abs(grads_J))
            grad_abs = abs(grad_samples)
            ranges = [np.max(grad_samples[j,:100]) - np.min(grad_samples[j,:100]) for j in range(m)]
            d,f,eps_star,N_star = stat_bound(R,M_phi,sigma,abs(grads_J),sample_vars,c,ranges)
            del grad_samples
               
            #Adaptive step-size
            actual_eps = d/math.sqrt(N) + f/N
            alpha = (infgrad - actual_eps)**2/(2*c[k]*(infgrad + actual_eps)**2) 
            if verbose>0:
                    print 'alpha:', alpha
            
            #Record
            print 'k', k
            theta = pol.get_weights()
            print theta
            if record:
                fp.write("{} {} {} {} {} {} {}\n".format(iteration,infgrad,J_est,deltaJ_est,nav,N_tot,theta))         

            #Update
            alpha_vect = np.zeros((m,),dtype=np.float32)
            alpha_vect[k] = alpha
            #alpha_vect = np.ones(m)*alpha_fix/iteration**.5#alpha_vect[k] = alpha
            pol.update(grads_J*alpha_vect)
            
            #Adaptive batch-size (used for next batch)
            if verbose>0:
                print 'epsilon:', eps_star, 'maxgrad:', infgrad, 'f:', f
                if eps_star>=infgrad:
                    print 'eps too high!'
            #N = N_fix#
            N = min(N_max,max(N_min,N_star)) 
            print 'Next N:', N
        
            #Meta
            #pol.save_weights(idx=40)
            if verbose>0:
                print 'time:', time.time() - start, '\n'
            N_tot+=N
            if N_tot>N_maxtot:
                print "Max N reached"
                break
            #if iteration>=max_iter:
            #    print "Max iteration reached"
            #    break

        #pol.save_weights(idx=40)
              
    #Cleanup 
    print '\nStopped after',iteration,'iterations'
    if record:
        fp.close()
