import os
import re
import numpy as np
import pycircstat
import numexpr
import pandas as pd

from scipy.signal import hilbert
from scipy.stats import ttest_ind
from sklearn.decomposition import PCA
from joblib import Parallel, delayed

# bunch of matplotlib stuff
import matplotlib.pyplot as plt
import matplotlib.cm as cmx
import matplotlib.colors as clrs
import matplotlib as mpl
from mpl_toolkits.axes_grid1 import make_axes_locatable

# for brain plotting
import nilearn.plotting as ni_plot

from miller_ecog_tools.SubjectLevel.par_funcs import par_find_peaks_by_chan, my_local_max
from ptsa.data.filters import MorletWaveletFilter
from miller_ecog_tools.Utils import RAM_helpers
from miller_ecog_tools.SubjectLevel.subject_analysis import SubjectAnalysisBase
from miller_ecog_tools.SubjectLevel.subject_ram_eeg_data import SubjectRamEEGData


class SubjectTravelingWaveAnalysis(SubjectAnalysisBase, SubjectRamEEGData):
    """
    Subclass of SubjectAnalysis and SubjectRamEEGData.

    Meant to be run as a SubjectAnalysisPipeline with SubjectOscillationClusterAnalysis as the first step.
    Uses the results of SubjectOscillationClusterAnalysis to compute traveling wave statistics on the clusters.
    """

    # when band passing the EEG, don't allow the frequency range to go below this value
    LOWER_MIN_FREQ = 0.5

    def __init__(self, task=None, subject=None, montage=0):
        super(SubjectTravelingWaveAnalysis, self).__init__(task=task, subject=subject, montage=montage)

        # string to use when saving results files
        self.res_str = 'trav_waves.p'

        # when computing bandpass, plus/minus this number of frequencies
        self.hilbert_half_range = 1.5

        # time period on which to compute cluster statistics
        self.cluster_stat_start_time = 0
        self.cluster_stat_end_time = 1600

        # optional recall_filter_func. When present, will run SME at the bandpass frequency
        self.recall_filter_func = None

    def _generate_res_save_path(self):
        self.res_save_dir = os.path.join(os.path.split(self.save_dir)[0], self.__class__.__name__ + '_res')

    def analysis(self):
        """
        For each cluster in res['clusters']:

        1.

        """

        # make sure we have data
        if self.subject_data is None:
            print('%s: compute or load data first with .load_data()!' % self.subject)
            return

        # we must have 'clusters' in self.res
        if 'clusters' in self.res:
            self.res['traveling_waves'] = {}

            # get cluster names from dataframe columns
            cluster_names = list(filter(re.compile('cluster[0-9]+').match, self.res['clusters'].columns))

            # get circular-linear regression parameters
            theta_r, params = self.compute_grid_parameters()

            # compute cluster stats for each cluster
            for this_cluster_name in cluster_names:
                cluster_res = {}

                # get the names of the channels in this cluster
                cluster_elecs = self.res['clusters'][self.res['clusters'][this_cluster_name].notna()]['label']

                # for the channels in this cluster, bandpass and then hilbert to get the phase info
                phase_data, power_data, cluster_mean_freq = self.compute_hilbert_for_cluster(this_cluster_name)

                # reduce to only time inverval of interest
                time_inds = (phase_data.time >= self.cluster_stat_start_time) & (
                        phase_data.time <= self.cluster_stat_end_time)
                phase_data = phase_data[:, :, time_inds]

                # get electrode coordinates in 2d
                norm_coords = self.compute_2d_elec_coords(this_cluster_name)

                # run the cluster stats for time-averaged data
                mean_rel_phase = pycircstat.mean(phase_data.data, axis=2)
                mean_cluster_wave_ang, mean_cluster_wave_freq, mean_cluster_r2_adj = \
                    circ_lin_regress(mean_rel_phase.T, norm_coords, theta_r, params)
                cluster_res['mean_cluster_wave_ang'] = mean_cluster_wave_ang
                cluster_res['mean_cluster_wave_freq'] = mean_cluster_wave_freq
                cluster_res['mean_cluster_r2_adj'] = mean_cluster_r2_adj

                # and run it for each time point
                num_times = phase_data.shape[-1]
                data_as_list = zip(phase_data.T, [norm_coords] * num_times, [theta_r] * num_times, [params] * num_times)
                res_as_list = Parallel(n_jobs=12, verbose=5)(delayed(circ_lin_regress)(x[0].data, x[1], x[2], x[3])
                                                             for x in data_as_list)
                cluster_res['cluster_wave_ang'] = np.stack([x[0] for x in res_as_list], axis=0).astype('float32')
                cluster_res['cluster_wave_freq'] = np.stack([x[1] for x in res_as_list], axis=0).astype('float32')
                cluster_res['cluster_r2_adj'] = np.stack([x[2] for x in res_as_list], axis=0).astype('float32')
                cluster_res['mean_freq'] = cluster_mean_freq
                cluster_res['channels'] = cluster_elecs.values
                cluster_res['time'] = phase_data.time.data
                cluster_res['phase_data'] = pycircstat.mean(phase_data, axis=1).astype('float32')

                # finally, compute the subsequent memory effect
                if hasattr(self, 'recall_filter_func') and callable(self.recall_filter_func):
                    delta_z, ts, ps = self.compute_sme_for_cluster(power_data)
                    cluster_res['sme_t'] = ts
                    cluster_res['sme_z'] = delta_z
                    cluster_res['ps'] = ps
                self.res['traveling_waves'][this_cluster_name] = cluster_res

        else:
            print('{}: self.res must have a clusters entry before running.'.format(self.subject))
            return

    def compute_grid_parameters(self):
        """
        Angle and phase offsets over which to compute the traveling wave statistics. Consider making these
        modifiable.
        """

        thetas = np.radians(np.arange(0, 356, 5))
        rs = np.radians(np.arange(0, 18.1, .5))
        theta_r = np.stack([(x, y) for x in thetas for y in rs])
        params = np.stack([theta_r[:, 1] * np.cos(theta_r[:, 0]), theta_r[:, 1] * np.sin(theta_r[:, 0])], -1)
        return theta_r, params

    def compute_hilbert_for_cluster(self, this_cluster_name):

        # first, get the eeg for just channels in cluster
        cluster_rows = self.res['clusters'][this_cluster_name].notna()
        cluster_elec_labels = self.res['clusters'][cluster_rows]['label']
        cluster_eeg = self.subject_data[:, np.in1d(self.subject_data.channel, cluster_elec_labels)]

        # bandpass eeg at the mean frequency, making sure the lower frequency isn't too low
        cluster_mean_freq = self.res['clusters'][cluster_rows][this_cluster_name].mean()
        cluster_freq_range = [cluster_mean_freq - self.hilbert_half_range, cluster_mean_freq + self.hilbert_half_range]
        if cluster_freq_range[0] < SubjectTravelingWaveAnalysis.LOWER_MIN_FREQ:
            cluster_freq_range[0] = SubjectTravelingWaveAnalysis.LOWER_MIN_FREQ
        filtered_eeg = RAM_helpers.band_pass_eeg(cluster_eeg, cluster_freq_range)
        filtered_eeg = filtered_eeg.transpose('channel', 'event', 'time')

        # run the hilbert transform
        complex_hilbert_res = hilbert(filtered_eeg.data, N=filtered_eeg.shape[-1], axis=-1)

        # compute the phase of the filtered eeg
        phase_data = filtered_eeg.copy()
        phase_data.data = np.unwrap(np.angle(complex_hilbert_res))

        # compute the power
        power_data = filtered_eeg.copy()
        power_data.data = np.abs(complex_hilbert_res) ** 2

        # compute mean phase and phase difference between ref phase and each electrode phase
        ref_phase = pycircstat.mean(phase_data.data, axis=0)
        phase_data.data = pycircstat.cdiff(phase_data.data, ref_phase)
        return phase_data, power_data, cluster_mean_freq

    def compute_2d_elec_coords(self, this_cluster_name):

        # compute PCA of 3d electrode coords to get 2d coords
        cluster_rows = self.res['clusters'][this_cluster_name].notna()
        xyz = self.res['clusters'][cluster_rows][['x', 'y', 'z']].values
        xyz -= np.mean(xyz, axis=0)
        pca = PCA(n_components=3)
        norm_coords = pca.fit_transform(xyz)[:, :2]
        return norm_coords

    def compute_sme_for_cluster(self, power_data):
        # zscore the data by session
        z_data = RAM_helpers.zscore_by_session(power_data.transpose('event', 'channel', 'time'))

        # compare the recalled and not recalled items
        recalled = self.recall_filter_func(self.subject_data)
        delta_z = np.nanmean(z_data[recalled], axis=0) - np.nanmean(z_data[~recalled], axis=0)
        ts, ps, = ttest_ind(z_data[recalled], z_data[~recalled])
        return delta_z, ts, ps


    def plot_cluster_stats(self, cluster_name, vmin=None, vmax=None):

        # load data to get the time axis
        #     self.load_data()
        time_inds = (self.res['traveling_waves'][cluster_name]['time'] >= self.cluster_stat_start_time) & (
                self.res['traveling_waves'][cluster_name]['time'] <= self.cluster_stat_end_time)
        time_axis = self.res['traveling_waves'][cluster_name]['time']
        #     self.unload_data()

        # figure parameters
        fig, (ax1, ax2, ax3) = plt.subplots(3, 1)
        fig.set_size_inches(15, 15)
        mpl.rcParams['xtick.labelsize'] = 18
        mpl.rcParams['ytick.labelsize'] = 18

        # get all regions in clusters
        cluster_rows = self.res['clusters'][cluster_name].notna()
        regions = self.get_electrode_roi()[cluster_rows]['merged_col'].unique()
        regions_str = ', '.join(regions)

        # plot 1: locaiton of cluster on brain
        xyz = self.res['clusters'][cluster_rows][['x', 'y', 'z']].values

        mean_r2 = np.nanmean(self.res['traveling_waves'][cluster_name]['cluster_r2_adj'], axis=1)
        argmax_r2 = np.argmax(mean_r2)
        phases = self.res['traveling_waves'][cluster_name]['phase_data'][:, argmax_r2]
        phases = (phases + np.pi) % (2 * np.pi) - np.pi
        phases *= 180. / np.pi
        phases -= phases.min() - 1
        #     phases = np.rad2deg(phases)

        print(phases)
        colors = np.stack([[0., 0., 0., 0.]] * len(phases))
        cm = plt.get_cmap('jet')
        cNorm = clrs.Normalize(vmin=np.nanmin(phases) if vmin is None else vmin,
                               vmax=np.nanmax(phases) if vmax is None else vmax)
        colors[~np.isnan(phases)] = cmx.ScalarMappable(norm=cNorm, cmap=cm).to_rgba(phases[~np.isnan(phases)])

        ni_plot.plot_connectome(np.eye(xyz.shape[0]), xyz,
                                node_kwargs={'alpha': 0.7, 'edgecolors': None},
                                node_size=45, node_color=colors, display_mode='lzr',
                                axes=ax1)

        mean_freq = self.res['traveling_waves'][cluster_name]['mean_freq']
        plt.suptitle('{0} ({1:.2f} Hz): {2}'.format(self.subject, mean_freq, regions_str), y=.9)

        divider = make_axes_locatable(ax1)
        cax = divider.append_axes('right', size='6%', pad=15)
        cb1 = mpl.colorbar.ColorbarBase(cax, cmap='jet',
                                        norm=cNorm,
                                        orientation='vertical')
        #     cax.yaxis.set_ticks_position('right')
        #     cb1.ax.tick_params(axis='y',direction='out',labelpad=30)
        #     cb1.set_label('Phase', fontsize=20)
        cb1.ax.tick_params(labelsize=14)

        # plot 2: resultant vector length as a function of time
        rvl = pycircstat.resultant_vector_length(self.res['traveling_waves'][cluster_name]['cluster_wave_ang'], axis=1)
        ax2.plot(time_axis, rvl, lw=2)
        ax2.set_ylabel('RVL', fontsize=20)

        # plot 3: r2 over time
        ax3.plot(time_axis, np.nanmean(self.res['traveling_waves'][cluster_name]['cluster_r2_adj'], axis=1), lw=2)
        ax3.set_xlabel('Time (ms)', fontsize=20)
        ax3.set_ylabel('mean($R^{2}$)', fontsize=20)
        return fig

    def get_electrode_roi(self):

        if 'stein.region' in self.elec_info:
            region_key1 = 'stein.region'
        elif 'locTag' in self.elec_info:
            region_key1 = 'locTag'
        else:
            region_key1 = ''

        if 'ind.region' in self.elec_info:
            region_key2 = 'ind.region'
        else:
            region_key2 = 'indivSurf.anatRegion'

        hemi_key = 'ind.x' if 'ind.x' in subj.elec_info else 'indivSurf.x'
        if self.elec_info[hemi_key].iloc[0] == 'NaN':
            hemi_key = 'tal.x'
        regions = self.bin_electrodes_by_region(elec_column1=region_key1 if region_key1 else region_key2,
                                                elec_column2=region_key2,
                                                x_coord_column=hemi_key)
        regions['merged_col'] = regions['hemi'] + '-' + regions['region']
        return regions


def circ_lin_regress(phases, coords, theta_r, params):
    """

    """

    n = phases.shape[1]
    pos_x = np.expand_dims(coords[:, 0], 1)
    pos_y = np.expand_dims(coords[:, 1], 1)

    # compute predicted phases for angle and phase offset
    x = np.expand_dims(phases, 2) - params[:, 0] * pos_x - params[:, 1] * pos_y

    # Compute resultant vector length. This is faster than calling pycircstat.resultant_vector_length
    x1 = numexpr.evaluate('sum(cos(x) / n, axis=1)')
    x1 = numexpr.evaluate('x1 ** 2')
    x2 = numexpr.evaluate('sum(sin(x) / n, axis=1)')
    x2 = numexpr.evaluate('x2 ** 2')
    Rs = numexpr.evaluate('-sqrt(x1 + x2)')

    # for each time and event, find the parameters with the smallest -R
    min_vals = theta_r[np.argmin(Rs, axis=1)]

    sl = min_vals[:, 1] * np.array([np.cos(min_vals[:, 0]), np.sin((min_vals[:, 0]))])
    offs = np.arctan2(np.sum(np.sin(phases.T - sl[0, :] * pos_x - sl[1, :] * pos_y), axis=0),
                      np.sum(np.cos(phases.T - sl[0, :] * pos_x - sl[1, :] * pos_y), axis=0))
    pos_circ = np.mod(sl[0, :] * pos_x + sl[1, :] * pos_y + offs, 2 * np.pi)

    # compute circular correlation coefficient between actual phases and predicited phases
    circ_corr_coef = pycircstat.corrcc(phases.T, pos_circ, axis=0)

    # compute adjusted r square
    r2_adj = circ_corr_coef ** 2

    wave_ang = min_vals[:, 0]
    wave_freq = min_vals[:, 1]

    return wave_ang, wave_freq, r2_adj

