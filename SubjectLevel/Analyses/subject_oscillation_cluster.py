"""
"""
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.gridspec
import ram_data_helpers
from sklearn import linear_model
import statsmodels.api as sm
from scipy.stats import ttest_ind, sem
from copy import deepcopy
from SubjectLevel.subject_analysis import SubjectAnalysis
from tarjan import tarjan
# from SubjectLevel.Analyses import subject_SME
# from SubjectLevel.Analyses.subject_SME import SubjectSME as SME
from SubjectLevel.par_funcs import par_find_peaks_by_ev
from scipy.spatial.distance import pdist, squareform
from scipy.signal import argrelmax
from tqdm import tqdm
from joblib import Parallel, delayed

import pdb

class SubjectElecCluster(SubjectAnalysis):
    """

    """

    def __init__(self, task=None, subject=None, montage=0, use_json=True):
        super(SubjectAnalysis, self).__init__(task=task, subject=subject, montage=montage, use_json=use_json)

        self.task_phase_to_use = ['enc']  # ['enc'] or ['rec']
        self.recall_filter_func = ram_data_helpers.filter_events_to_recalled_norm
        self.rec_thresh = None

        # string to use when saving results files
        self.res_str = 'elec_cluster.p'

        # default frequency settings
        self.feat_type = 'power'
        self.freqs = np.logspace(np.log10(2), np.log10(32), 129)

        # window size to find clusters (in Hz)
        self.cluster_freq_range = 2.

        # spatial distance considered near
        self.near_dist = 15.

        # number of electrodes needed to be considered a clust
        self.min_num_elecs = 4

    def analysis(self):
        """
        Fits a robust regression model to the power spectrum of each electrode in order to get the slope and intercept.
        This fits every event individually in addition to each electrode, so it's a couple big loops. Sorry. It seems
        like you should be able to it all with one call by having multiple columns in y, but the results are different
        than looping, so..
        """

        # Get recalled or not labels
        self.filter_data_to_task_phases(self.task_phase_to_use)
        recalled = self.recall_filter_func(self.task, self.subject_data.events.data, self.rec_thresh)



        # compute frequency bins
        window_centers = np.arange(self.freqs[0], self.freqs[-1] + .001, .1)
        windows = [(x - self.cluster_freq_range / 2., x + self.cluster_freq_range / 2.) for x in window_centers]
        window_bins = np.stack([(self.freqs >= x[0]) & (self.freqs <= x[1]) for x in windows], axis=0)



        # distance matrix for all electrodes
        elec_dists = squareform(pdist(np.stack(self.elec_xyz_indiv)))
        near_adj_matr = (elec_dists < 15.) & (elec_dists > 0.)




        # noramlize power spectra
        p_spect = deepcopy(self.subject_data)
        p_spect = self.normalize_spectra(p_spect)

        mean_p_spect = p_spect.mean(dim='events')

        rec_p_spect = p_spect[recalled].mean(dim='events')
        rec_peaks = par_find_peaks_by_ev(rec_p_spect)
        self.rec_clusters = self.find_clusters_from_peaks([rec_peaks], near_adj_matr, window_bins, window_centers)

        nrec_p_spect = p_spect.mean(dim='events')
        nrec_peaks = par_find_peaks_by_ev(nrec_p_spect)
        self.nrec_clusters = self.find_clusters_from_peaks([nrec_peaks], near_adj_matr, window_bins, window_centers)

        #
        # print('%s: finding peaks for %d events and %d electrodes.' % (self.subj, self.subject_data.shape[0], self.subject_data.shape[2]))
        #
        # if self.pool is None:
        #     peaks = map(par_find_peaks_by_ev, tqdm(self.subject_data))
        # else:
        #     peaks = self.pool.map(par_find_peaks_by_ev, self.subject_data)
        # peaks = np.stack(peaks, axis=0)
        #
        #
        #
        # # loop over each event and 1) for each electrode, determine if there is a peak in a given bin; 2) after
        # # computing the binned peaks for all electrodes, count the number of electrodes with peaks in each bin and find
        # # local maxima; 3) for each frequency at a local max, find clusters using .near_dist threshold and tarjan's
        # # algo.
        #
        # # binned_peak_by_elec = np.zeros((peaks.shape[0], len(windows), peaks.shape[2])).astype(bool)
        # # peak_count_by_freqs = np.zeros((peaks.shape[0], len(windows)))
        #
        # # what analyses do we want to do.
        # # simplest, is there a cluster for an event and frequency bin
        # clust_count =  np.zeros((peaks.shape[0], len(windows)))
        # self.find_clusters_from_peaks(peaks, near_adj_matr, window_bins)
        # self.clust_count = clust_count


    def find_clusters_from_peaks(self, peaks, near_adj_matr, window_bins, window_centers):

        all_clusters = {k: [] for k in window_centers}
        for i, ev in enumerate(peaks):

            # bin peaks, count them up, and find the peaks (of the peaks...)
            # pdb.set_trace()
            binned_peaks = np.stack([np.any(ev[x], axis=0) for x in window_bins], axis=0)
            peak_freqs = argrelmax(binned_peaks.sum(axis=1))[0]

            # for each peak frequency, identify clusters
            for this_peak_freq in peak_freqs:
                near_this_ev = near_adj_matr.copy()
                near_this_ev[~binned_peaks[this_peak_freq]] = False
                near_this_ev[:, ~binned_peaks[this_peak_freq]] = False

                # use targan algorithm to find the clusters
                graph = {}
                for elec, row in enumerate(near_this_ev):
                    graph[elec] = np.where(row)[0]
                clusters = tarjan(graph)

                # only keep clusters with enough electrodes
                good_clusters = np.array([len(x) for x in clusters]) >= self.min_num_elecs
                for good_cluster in np.where(good_clusters)[0]:
                    all_clusters[window_centers[this_peak_freq]].append(clusters[good_cluster])
        return dict((k, v) for k, v in all_clusters.items() if v)


    def normalize_spectra(self, X):
        """
        Normalize the power spectra by session.
        """
        uniq_sessions = np.unique(self.subject_data.events.data['session'])
        for sess in uniq_sessions:
            sess_event_mask = (self.subject_data.events.data['session'] == sess)
            for phase in self.task_phase_to_use:
                task_mask = self.task_phase == phase

                m = np.mean(X[sess_event_mask & task_mask], axis=1)
                m = np.mean(m, axis=0)
                s = np.std(X[sess_event_mask & task_mask], axis=1)
                s = np.mean(s, axis=0)
                X[sess_event_mask & task_mask] = (X[sess_event_mask & task_mask] - m) / s
        return X












