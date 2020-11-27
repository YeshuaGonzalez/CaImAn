#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Apr 19 14:50:09 2019

@author: @caichangjia adapt based on Matlab code provided by Kaspar Podgorski and Amrita Singh
"""
import logging
import matplotlib.pyplot as plt
import numpy as np
from scipy import signal
from scipy import stats    
from scipy.ndimage.filters import gaussian_filter1d
from scipy.sparse.linalg import svds
from sklearn.linear_model import Ridge
from skimage.morphology import dilation
from skimage.morphology import disk
import cv2
import caiman as cm
from caiman.base.movies import movie

# %%
def volspike(pars):
    """ Function for finding spikes of single neuron with given ROI in
        voltage imaging. Use function denoise_spikes to find spikes
        from one dimensional signal, and use ridge regression to find the
        best weight. Do these two steps iteratively to find
        best spike times.

        Args:
            pars: list
                fnames: str
                    name of the memory mapping file in C order

                fr: int
                    frame rate of the movie

                cell_n: int
                    index of the cell processing

                ROIs: 3-d array
                    all regions of interests

                weights: 3-d array
                    spatial weights of different cells generated by previous data blocks as initialization

                args: dictionary
                    context_size: int
                        number of pixels surrounding the ROI to use as context

                    censor_size: int
                        number of pixels surrounding the ROI to censor from the background PCA; roughly
                        the spatial scale of scattered/dendritic neural signals, in pixels
                        
                    flip_signal: boolean
                        whether to flip signal upside down for spike detection 
                        True for voltron, False for others

                    hp_freq_pb: float
                        high-pass frequency for removing photobleaching    
                    
                    nPC_bg: int
                        number of principle components used for background subtraction
                        
                    ridge_bg: float
                        regularization strength for ridge regression in background removal 

                    hp_freq: float
                        high-pass cutoff frequency to filter the signal after computing the trace
                        
                    clip: int
                        maximum number of spikes for producing templates

                    threshold_method: str
                        'simple' or 'adaptive_threshold' method for thresholding signals
                        'simple' method threshold based on estimated noise level 
                        'adaptive_threshold' method threshold based on estimated peak distribution
                        
                    min_spikes: int
                        minimal number of spikes to be detected

                    threshold: float
                        threshold for spike detection in 'simple' threshold method 
                        The real threshold is the value multiplied by the estimated noise level

                    sigmas: 1-d array
                        spatial smoothing radius imposed on high-pass filtered 
                        movie only for finding weights

                    n_iter: int
                        number of iterations alternating between estimating spike times
                        and spatial filters
                        
                    weight_update: str
                        'ridge' or 'NMF' for weight update
                        
                    do_plot: boolean
                        if Ture, plot trace of signals and spiketimes, 
                        peak triggered average, histogram of heights in the last iteration

                    do_cross_val: boolean
                        whether to use cross validation to optimize regression regularization parameters
                        
                    sub_freq: float
                        frequency for subthreshold extraction


        Returns:
            output: dictionary
                cell_n: int
                    index of cell        
            
                t: 1-d array
                    trace without applying whitened matched filter
                    
                ts: 1-d array
                    trace after applying whitened matched filter

                t_rec: 1-d array
                    reconstructed signal of the neuron
                
                t_sub: 1-d array
                    subthreshold signal of the neuron
                    
                spikes: 1-d array
                    spike time of the neuron

                num_spikes: list
                    number of spikes detected in each iteration 
                    
                low_spikes: boolean
                    True if detected number spikes is less than min_spikes 
                         
                template: 1-d array
                    temporal template of the neuron
                
                snr: float
                    signal to noise ratio of the processed signal
                    
                thresh: float
                    threshold of the signal

                spatial_filter: 2-d array
                    spatial filter of the neuron in the whole FOV
                    
                weights: 2-d array
                    ridge regression coefficients for fitting reconstructed signal
                
                locality: boolean
                    False if the maximum of spatial filter is not in the initial ROI
                
                context_coord: 2-d array
                    boundary of context region in x,y coordinates
                
                mean_im: 1-d array
                    mean of the signal in ROI after removing photobleaching, used for 
                    producing F0
                    
                F0: 1-d array
                    baseline signal
                    
                dFF: 1-d array
                    scaled signal
                    
                rawROI: dictionary
                    including the result after the first spike extraction
    """
    # load parameters
    fnames = pars[0]
    fr = pars[1]
    cell_n = pars[2]
    bw = pars[3]    
    weights_init = pars[4]    
    args = pars[5]
    window_length = fr * 0.02 # window length for temporal templates
    output = {}
    output['rawROI'] = {}
    print(f'Now processing cell number {cell_n}')
    
    # load the movie in C order mmap file
    Yr, dims, T = cm.load_memmap(fnames)
    if bw.shape == dims:
        images = np.reshape(Yr.T, [T] + list(dims), order='F')
    else:
        raise Exception('Dimensions of movie and ROIs do not accord')
        
    # extract relevant region and align
    bwexp = dilation(bw, np.ones([args['context_size'], args['context_size']]), shift_x=True, shift_y=True)
    Xinds = np.where(np.any(bwexp > 0, axis=1) > 0)[0]
    Yinds = np.where(np.any(bwexp > 0, axis=0) > 0)[0]
    bw = bw[Xinds[0]:Xinds[-1] + 1, Yinds[0]:Yinds[-1] + 1]
    notbw = 1 - dilation(bw, disk(args['censor_size']))
    data = np.array(images[:, Xinds[0]:Xinds[-1] + 1, Yinds[0]:Yinds[-1] + 1])
    bw = (bw > 0)
    notbw = (notbw > 0)
    ref = np.median(data[:500, :, :], axis=0)
    bwexp[Xinds[0]:Xinds[-1] + 1, Yinds[0]:Yinds[-1] + 1] = True

    # visualize ROI
    visualize_ROI = False
    if visualize_ROI:
        fig = plt.figure()
        plt.subplot(131);plt.imshow(ref);plt.axis('image');plt.xlabel('mean Intensity')
        plt.subplot(132);plt.imshow(bw);plt.axis('image');plt.xlabel('initial ROI')
        plt.subplot(133);plt.imshow(notbw);plt.axis('image');plt.xlabel('background')
        fig.suptitle('ROI selection')
        plt.show()
    
    # flip the signal if necessary
    if args['flip_signal']==True:
        data = -data
    else:
        pass
    
    # remove photobleaching effect by high pass filtering signal
    output['mean_im'] = np.mean(data, axis=0)
    data = np.reshape(data, (data.shape[0], -1))
    data = data - np.mean(data, 0)
    data = data - np.mean(data, 0)   #do again because of numeric issues
    data_hp = signal_filter(data.T, args['hp_freq_pb'], fr).T  
    data_lp = data - data_hp

    # initial trace
    if weights_init is None:
        t0 = np.nanmean(data_hp[:, bw.ravel()], 1)
    else:
        t0 = np.matmul(data_hp, weights_init[1:])  
    t0 = t0 - np.mean(t0)

    # remove any variance in trace that can be predicted from the background principal components
    Ub, Sb, Vb = svds(data_hp[:, notbw.ravel()], args['nPC_bg'])
    alpha = args['nPC_bg'] * args['ridge_bg']    # square of F-norm of Ub is equal to number of principal components
    reg = Ridge(alpha=alpha, fit_intercept=False, solver='lsqr').fit(Ub, t0)
    t0 = np.double(t0 - np.matmul(Ub, reg.coef_))
    
    # find out spikes of initial trace
    ts, spikes, t_rec, templates, low_spikes, thresh = denoise_spikes(t0, 
                                          window_length, fr, hp_freq=args['hp_freq'], clip=args['clip'],
                                          threshold_method=args['threshold_method'], threshold=args['threshold'], 
                                          min_spikes=args['min_spikes'], do_plot=False)

    output['rawROI']['t'] = t0.copy()
    output['rawROI']['ts'] = ts.copy()
    output['rawROI']['spikes'] = spikes.copy()
    output['rawROI']['spatial_filter'] = bw.copy()
    output['rawROI']['t'] = output['rawROI']['t'] * np.mean(t0[output['rawROI']['spikes']]) / np.mean(
        output['rawROI']['t'][output['rawROI']['spikes']])  # correct shrinkage
    output['rawROI']['templates'] = templates
    num_spikes = [spikes.shape[0]]

    # prebuild the regression matrix generate a predictor for ridge regression
    pred = np.empty_like(data_hp)
    pred[:] = data_hp
    pred = np.hstack((np.ones((data_hp.shape[0], 1), dtype=np.single), np.reshape
    (movie.gaussian_blur_2D(np.reshape(pred,
                                       (data_hp.shape[0], ref.shape[0], ref.shape[1])),
                            kernel_size_x=7, kernel_size_y=7, kernel_std_x=1.5,
                            kernel_std_y=1.5, borderType=cv2.BORDER_REPLICATE), data_hp.shape)))

    # cross-validation of regularized regression parameters
    lambdamax = np.single(np.linalg.norm(pred[:, 1:], ord='fro') ** 2)
    lambdas = lambdamax * np.logspace(-4, -2, 3)
    
    if args['do_cross_val']:
        # need to add
        logging.warning('doing cross validation')
        raise Exception('cross validation option is not availble yet')
    else:
        s_max = 1
        l_max = 2
        sigma = args['sigmas'][s_max]
    
    recon = np.empty_like(data_hp)
    recon[:] = data_hp
    recon = np.hstack((np.ones((data_hp.shape[0], 1), dtype=np.single), np.reshape
    (movie.gaussian_blur_2D(np.reshape(recon,
                                       (data_hp.shape[0], ref.shape[0], ref.shape[1])),
                            kernel_size_x=np.int(2 * np.ceil(2 * sigma) + 1),
                            kernel_size_y=np.int(2 * np.ceil(2 * sigma) + 1),
                            kernel_std_x=sigma, kernel_std_y=sigma,
                            borderType=cv2.BORDER_REPLICATE), data_hp.shape)))

    # do the following two steps for several iterations: update spatial filter, detect 
    # best spike times
    for iteration in range(args['n_iter']):
        if iteration == args['n_iter'] - 1:
            do_plot = args['do_plot']
        else:
            do_plot = False
            
        # update spatial weights
        tr = np.single(t_rec.copy())
        if args['weight_update'] == 'NMF':
            C = np.array([tr, np.ones_like(tr)])  # constant baselines as 2nd component
            CCt = C.dot(C.T)
            CY = C.dot(recon[:, 1:])
            A = np.maximum(np.linalg.inv(CCt).dot(CY), 0)
            for _ in range(5):
                for m in range(2):
                    A[m] += (CY[m] - CCt[m].dot(A)) / CCt[m, m]
                    if m == 0:
                        A[m] = np.maximum(A[m], 0)
            weights = np.concatenate([[0], A[0]])
        elif args['weight_update'] == 'ridge':
            Ri = Ridge(alpha=lambdas[l_max], fit_intercept=True, solver='lsqr')
            Ri.fit(recon, tr)
            weights = Ri.coef_
            weights[0] = Ri.intercept_

        # compute spatial filter
        spatial_filter = np.empty_like(weights)
        spatial_filter[:] = weights
        spatial_filter = movie.gaussian_blur_2D(np.reshape(spatial_filter[1:],
                                                          ref.shape, order='C')[np.newaxis, :, :],
                                               kernel_size_x=np.int(2 * np.ceil(2 * sigma) + 1),
                                               kernel_size_y=np.int(2 * np.ceil(2 * sigma) + 1),
                                               kernel_std_x=sigma, kernel_std_y=sigma,
                                               borderType=cv2.BORDER_REPLICATE)[0]

        # compute new signal            
        t = np.matmul(recon, weights)
        t = t - np.mean(t)

        # ridge regression to remove background components
        b = Ridge(alpha=alpha, fit_intercept=False, solver='lsqr').fit(Ub, t).coef_
        t = t - np.matmul(Ub, b)

        # correct shrinkage
        t = np.double(t * np.mean(t0[spikes]) / np.mean(t[spikes]))

        # generate the new trace and the new denoised trace
        ts, spikes, t_rec, templates, low_spikes, thresh = denoise_spikes(t, 
                    window_length, fr,  hp_freq=args['hp_freq'], clip=args['clip'],
                    threshold_method=args['threshold_method'], threshold=args['threshold'], 
                    min_spikes=args['min_spikes'], do_plot=do_plot)
    
        num_spikes.append(spikes.shape[0])

    # compute SNR
    if len(spikes)>0:
        t = t - np.median(t)
        selectSpikes = np.zeros(t.shape)
        selectSpikes[spikes] = 1
        sgn = np.mean(t[selectSpikes > 0])
        ff1 = -t * (t < 0)
        Ns = np.sum(ff1 > 0)
        noise = np.sqrt(np.divide(np.sum(ff1**2), Ns)) 
        snr = sgn / noise
    else:
        snr = 0

    # locality test       
    matrix = np.matmul(np.transpose(pred[:, 1:]), t_rec)
    sigmax = np.sqrt(np.sum(np.multiply(pred[:, 1:], pred[:, 1:]), axis=0))
    sigmay = np.sqrt(np.dot(t_rec, t_rec))
    IMcorr = matrix / sigmax / sigmay
    maxCorrInROI = np.max(IMcorr[bw.ravel()])
    if np.any(IMcorr[notbw.ravel()] > maxCorrInROI):
        locality = False
    else:
        locality = True
    
    # spatial filter in FOV
    weights = np.reshape(weights[1:],ref.shape, order='C')
    weights_FOV = np.zeros(images.shape[1:])
    weights_FOV[Xinds[0]:Xinds[-1] + 1, Yinds[0]:Yinds[-1] + 1] = weights

    spatial = np.zeros(images.shape[1:])
    spatial[Xinds[0]:Xinds[-1] + 1, Yinds[0]:Yinds[-1] + 1] = spatial_filter

    # subthreshold activity extraction    
    t_sub = t.copy() - t_rec
    t_sub = signal_filter(t_sub, args['sub_freq'], fr, order=5, mode='low') 

    # output
    output['cell_n'] = cell_n
    output['t'] = t
    output['ts'] = ts
    output['t_rec'] = t_rec        
    output['t_sub'] = t_sub
    output['spikes'] = spikes
    output['low_spikes'] = low_spikes
    output['num_spikes'] = num_spikes
    output['templates'] = templates
    output['snr'] = snr
    output['thresh'] = thresh
    output['spatial_filter'] = spatial    
    output['weights'] = weights_FOV
    output['locality'] = locality    
    output['context_coord'] = np.transpose(np.vstack((Xinds[[0, -1]], Yinds[[0, -1]])))
    output['F0'] = np.abs(np.nanmean(data_lp[:, bw.flatten()] + output['mean_im'][bw][np.newaxis, :], 1))
    output['dFF'] = t / output['F0']
    output['rawROI']['dFF'] = output['rawROI']['t'] / output['F0']
    
    return output


def denoise_spikes(data, window_length, fr=400,  hp_freq=1,  clip=2000, threshold_method='simple', 
                   min_spikes=5, threshold=3.5,  do_plot=True):
    """ Function for finding spikes and the temporal filter given one dimensional signals.
        Use function whitened_matched_filter to denoise spikes. Two thresholding methods can be 
        chosen, 'simple' or 'adaptive thresholding'.

    Args:
        data: 1-d array
            one dimensional signal

        window_length: int
            length of window size for temporal filter

        fr: int
            number of samples per second in the video
            
        hp_freq: float
            high-pass cutoff frequency to filter the signal after computing the trace
            
        clip: int
            maximum number of spikes for producing templates

        threshold_method: str
            'simple' or 'adaptive_threshold' method for thresholding signals
            'simple' method threshold based on estimated noise level 
            'adaptive_threshold' method threshold based on estimated peak distribution
            
        min_spikes: int
            minimal number of spikes to be detected

        threshold: float
            threshold for spike detection in 'simple' threshold method 
            The real threshold is the value multiply estimated noise level

        do_plot: boolean
            if Ture, will plot trace of signals and spiketimes, peak triggered
            average, histogram of heights
            
    Returns:
        datafilt: 1-d array
            signals after whitened matched filter

        spikes: 1-d array
            record of time of spikes

        t_rec: 1-d array
            recovery of original signals

        templates: 1-d array
            temporal filter which is the peak triggered average

        low_spikes: boolean
            True if number of spikes is smaller than 30
            
        thresh2: float
            real threshold in second round of spike detection 
    """
    # high-pass filter the signal to remove part of subthreshold activity
    data = data - np.median(data)
    data = signal_filter(data, hp_freq, fr, order=5)
        
    low_spikes = False
    data = data - np.median(data)
    pks = data[signal.find_peaks(data, height=None)[0]]

    # find spikes    
    if threshold_method == 'simple':
        ff1 = -data * (data < 0)
        Ns = np.sum(ff1 > 0)
        std = np.sqrt(np.divide(np.sum(ff1**2), Ns)) 
        thresh = 3.5 * std
        locs = signal.find_peaks(data, height=thresh)[0]
        if len(locs) < min_spikes:
            logging.warning(f'less than {min_spikes} spikes are found, pick top {min_spikes} spikes')
            thresh = np.percentile(pks, 100 * (1 - min_spikes / len(pks)))
            locs = signal.find_peaks(data, height=thresh)[0]
            low_spikes = True
        elif ((len(locs) > clip) & (clip > 0)):
            logging.warning(f'Selecting top {clip} spikes for template')
            thresh = np.percentile(pks, 100 * (1 - clip / len(pks)))
            locs = signal.find_peaks(data, height=thresh)[0]
    elif threshold_method == 'adaptive_threshold':
        thresh, _, _, low_spikes = get_thresh(pks, clip, 0.25, min_spikes)
        locs = signal.find_peaks(data, height=thresh)[0]
    else:
        logging.warning("Error: threshold_method not found")
        raise Exception('Threshold_method not found!')

    # peak-traiggered average
    window = np.int64(np.arange(-window_length, window_length + 1, 1))
    locs = locs[np.logical_and(locs > (-window[0]), locs < (len(data) - window[-1]))]
    PTD = data[(locs[:, np.newaxis] + window)]
    PTA = np.median(PTD, 0)
    PTA = PTA - np.min(PTA)
    templates = PTA

    # whitened matched filter
    datafilt = whitened_matched_filter(data, locs, window)    
    datafilt = datafilt - np.median(datafilt)

    # spikes detected after filter
    pks2 = datafilt[signal.find_peaks(datafilt, height=None)[0]]
    if threshold_method == 'simple':
        ff1 = -datafilt * (datafilt < 0)
        Ns = np.sum(ff1 > 0)
        std2 = np.sqrt(np.divide(np.sum(ff1**2), Ns)) 
        thresh2 = threshold * std2
        spikes = signal.find_peaks(datafilt, height=thresh2)[0]
        
        if len(spikes) < min_spikes:
            low_spikes = True
            logging.warning(f'Less than {min_spikes} spikes were found. Picking top {min_spikes} spikes')
            thresh2 = np.percentile(pks2, 100 * (1 - min_spikes / len(pks2)))
            spikes = signal.find_peaks(datafilt, height=thresh2)[0]
    elif threshold_method == 'adaptive_threshold':
        thresh2, falsePosRate, detectionRate, _ = get_thresh(pks2, clip=0, pnorm=0.5, min_spikes=min_spikes)  # clip=0 means no clipping
        spikes = signal.find_peaks(datafilt, height=thresh2)[0]

    t_rec = np.zeros(datafilt.shape)
    t_rec[spikes] = 1
    t_rec = np.convolve(t_rec, PTA, 'same')   
    factor = np.mean(data[spikes]) / np.mean(datafilt[spikes])
    datafilt = datafilt * factor
    thresh2_normalized = thresh2 * factor
        
    if do_plot:
        plt.figure()
        plt.subplot(211)
        plt.hist(pks, 500)
        plt.axvline(x=thresh, c='r')
        plt.title('raw data')
        plt.subplot(212)
        plt.hist(pks2, 500)
        plt.axvline(x=thresh2, c='r')
        plt.title('after matched filter')
        plt.tight_layout()
        plt.show()

        plt.figure()
        plt.plot(np.transpose(PTD), c=[0.5, 0.5, 0.5])
        plt.plot(PTA, c='black', linewidth=2)
        plt.title('Peak-triggered average')
        plt.show()

        plt.figure()
        plt.subplot(211)
        plt.plot(data)
        plt.plot(locs, np.max(datafilt) * 1.1 * np.ones(locs.shape), color='r', marker='o', fillstyle='none',
                 linestyle='none')
        plt.plot(spikes, np.max(datafilt) * 1 * np.ones(spikes.shape), color='g', marker='o', fillstyle='none',
                 linestyle='none')
        plt.subplot(212)
        plt.plot(datafilt)
        plt.plot(locs, np.max(datafilt) * 1.1 * np.ones(locs.shape), color='r', marker='o', fillstyle='none',
                 linestyle='none')
        plt.plot(spikes, np.max(datafilt) * 1 * np.ones(spikes.shape), color='g', marker='o', fillstyle='none',
                 linestyle='none')
        plt.show()

    return datafilt, spikes, t_rec, templates, low_spikes, thresh2_normalized

def get_thresh(pks, clip, pnorm=0.5, min_spikes=30):
    """ Function for deciding threshold given heights of all peaks.

    Args:
        pks: 1-d array
            height of all peaks

        clip: int
            maximum number of spikes for producing templates

        pnorm: float, between 0 and 1, default is 0.5
            a variable deciding the amount of spikes chosen
            
        min_spikes: int
            minimal number of spikes to be detected

    Returns:
        thresh: float
            threshold for choosing spikes

        falsePosRate: float
            possibility of misclassify noise as real spikes

        detectionRate: float
            possibility of real spikes being detected

        low_spikes: boolean
            true if number of spikes is smaller than minimal value
    """
    # find median of the kernel density estimation of peak heights
    spread = np.array([pks.min(), pks.max()])
    spread = spread + np.diff(spread) * np.array([-0.05, 0.05])
    low_spikes = False
    pts = np.linspace(spread[0], spread[1], 2001)
    kde = stats.gaussian_kde(pks)
    f = kde(pts)    
    xi = pts
    center = np.where(xi > np.median(pks))[0][0]

    fmodel = np.concatenate([f[0:center + 1], np.flipud(f[0:center])])
    if len(fmodel) < len(f):
        fmodel = np.append(fmodel, np.ones(len(f) - len(fmodel)) * min(fmodel))
    else:
        fmodel = fmodel[0:len(f)]

    # adjust the model so it doesn't exceed the data:
    csf = np.cumsum(f) / np.sum(f)
    csmodel = np.cumsum(fmodel) / np.max([np.sum(f), np.sum(fmodel)])
    lastpt = np.where(np.logical_and(csf[0:-1] > csmodel[0:-1] + np.spacing(1), csf[1:] < csmodel[1:]))[0]
    if not lastpt.size:
        lastpt = center
    else:
        lastpt = lastpt[0]
    fmodel[0:lastpt + 1] = f[0:lastpt + 1]
    fmodel[lastpt:] = np.minimum(fmodel[lastpt:], f[lastpt:])

    # find threshold
    csf = np.cumsum(f)
    csmodel = np.cumsum(fmodel)
    csf2 = csf[-1] - csf
    csmodel2 = csmodel[-1] - csmodel
    obj = csf2 ** pnorm - csmodel2 ** pnorm
    maxind = np.argmax(obj)
    thresh = xi[maxind]

    if np.sum(pks > thresh) < min_spikes:
        low_spikes = True
        logging.warning(f'Few spikes were detected. Adjusting threshold to take {min_spikes} largest spikes')
        thresh = np.percentile(pks, 100 * (1 - min_spikes / len(pks)))
    elif ((np.sum(pks > thresh) > clip) & (clip > 0)):
        logging.warning(f'Selecting top {clip} spikes for template')
        thresh = np.percentile(pks, 100 * (1 - clip / len(pks)))

    ix = np.argmin(np.abs(xi - thresh))
    falsePosRate = csmodel2[ix] / csf2[ix]
    detectionRate = (csf2[ix] - csmodel2[ix]) / np.max(csf2 - csmodel2)
    return thresh, falsePosRate, detectionRate, low_spikes


def whitened_matched_filter(data, locs, window):
    """
    Function for using whitened matched filter to the original signal for better
    SNR. Use welch method to approximate the spectral density of the signal.
    Rescale the signal in frequency domain. After scaling, convolve the signal with
    peak-triggered-average to make spikes more prominent.
    
    Args:
        data: 1-d array
            input signal

        locs: 1-d array
            spike times

        window: 1-d array
            window with size of temporal filter

    Returns:
        datafilt: 1-d array
            signal processed after whitened matched filter
    
    """
    N = np.ceil(np.log2(len(data)))
    censor = np.zeros(len(data))
    censor[locs] = 1
    censor = np.int16(np.convolve(censor.flatten(), np.ones([1, len(window)]).flatten(), 'same'))
    censor = (censor < 0.5)
    noise = data[censor]

    _, pxx = signal.welch(noise, fs=2 * np.pi, window=signal.get_window('hamming', 1000), nfft=2 ** N, detrend=False,
                          nperseg=1000)
    Nf2 = np.concatenate([pxx, np.flipud(pxx[1:-1])])
    scaling_vector = 1 / np.sqrt(Nf2)

    cc = np.pad(data.copy(),(0,np.int(2**N-len(data))),'constant')    
    dd = (cv2.dft(cc,flags=cv2.DFT_SCALE+cv2.DFT_COMPLEX_OUTPUT)[:,0,:]*scaling_vector[:,np.newaxis])[:,np.newaxis,:]
    dataScaled = cv2.idft(dd)[:,0,0]
    PTDscaled = dataScaled[(locs[:, np.newaxis] + window)]
    PTAscaled = np.mean(PTDscaled, 0)
    datafilt = np.convolve(dataScaled, np.flipud(PTAscaled), 'same')
    datafilt = datafilt[:len(data)]
    return datafilt


def signal_filter(sg, freq, fr, order=3, mode='high'):
    """
    Function for high/low passing the signal with butterworth filter
    
    Args:
        sg: 1-d array
            input signal
            
        freq: float
            cutoff frequency
        
        order: int
            order of the filter
        
        mode: str
            'high' for high-pass filtering, 'low' for low-pass filtering
            
    Returns:
        sg: 1-d array
            signal after filtering            
    """
    normFreq = freq / (fr / 2)
    b, a = signal.butter(order, normFreq, mode)
    sg = np.single(signal.filtfilt(b, a, sg, padtype='odd', padlen=3 * (max(len(b), len(a)) - 1)))
    return sg