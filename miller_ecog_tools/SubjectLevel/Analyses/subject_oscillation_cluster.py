import os
import numpy as np

from tarjan import tarjan
from scipy.spatial.distance import pdist, squareform

from miller_ecog_tools.SubjectLevel.par_funcs import par_find_peaks_by_chan, my_local_max
from miller_ecog_tools.SubjectLevel.subject_analysis import SubjectAnalysisBase
from miller_ecog_tools.SubjectLevel.subject_eeg_data import SubjectEEGData


class SubjectOscillationClusterAnalysis(SubjectAnalysisBase, SubjectEEGData):
    """
    Subclass of SubjectAnalysis and SubjectEEGData that identifies clusters of electrodes in a subject that
    exhibit peaks in the power spectrum at similar frequencies.
    """

    res_str_tmp = 'elec_cluster_%d_mm_%d_elec_min_%s_elec_type_%s_sep_hemis_%.2f_cluster_range.p'
    attrs_in_res_str = ['elec_types_allowed', 'min_elec_dist', 'min_num_elecs', 'separate_hemis', 'cluster_freq_range']

    def __init__(self, task=None, subject=None, montage=0):
        super(SubjectOscillationClusterAnalysis, self).__init__(task=task, subject=subject, montage=montage)

        # string to use when saving results files
        self.res_str = SubjectOscillationClusterAnalysis.res_str_tmp

        # default frequency settings for identifying peaks
        self.freqs = np.logspace(np.log10(2), np.log10(32), 129)
        self.bipolar = False
        self.start_time = 0
        self.end_time = 1600

        # window size to find clusters (in Hz)
        self.cluster_freq_range = 2.

        # D: depths, G: grids, S: strips
        self.elec_types_allowed = ['D', 'G', 'S']

        # spatial distance considered near (mm)
        self.min_elec_dist = 15.

        # If True, osciallation clusters can't cross hemispheres
        self.separate_hemis = True

        # number of electrodes needed to be considered a cluster
        self.min_num_elecs = 4

        # elec_info column from which to extract x,y,z coordinates
        self.elec_pos_column = 'ind.'

    def _generate_res_save_path(self):
        self.res_save_dir = os.path.join(os.path.split(self.save_dir)[0], self.__class__.__name__+'_res')

    def analysis(self):
        """
        Identifies clusters of electrodes that have oscillations at similar frequencies, by:

        1. Determining the distance between all pairs of electrodes
        2. Computing the mean power spectra of each electrode
        3. Identifying at which each electrode has peaks over the 1/f background power
        4. Identifying clusters based on being spatially contiguous, as defined by spatial distance (.min_elec_dist),
           electrode type (.elec_types_allowed), hemisphere (.separate_hemis), and frequency contiguous
           (.cluster_freq_range). This uses the "targan" algorithm.
        """

        # make sure we have data
        if self.subject_data is None:
            print('%s: compute or load data first with .load_data()!' % self.subject)
            return

        # make distance matrix for all electrodes. If separating the hemispheres, move the hemispheres far apart
        xyz = self._get_elec_xyz()
        if self.separate_hemis:
            xyz[xyz[:, 0] < 0, 0] -= 100
        elec_dists = squareform(pdist(xyz))

        # figure out which pairs of electrodes are closer than the threshold
        near_adj_matr = (elec_dists < self.min_elec_dist) & (elec_dists > 0.)
        allowed_elecs = np.array([e in self.elec_types_allowed for e in self.elec_info['type']])

        # normalized power spectra
        p_spect = self.normalize_power_spectrum()

        # Compute mean power spectra across events, and then find where each electrode has peaks
        mean_p_spect = np.mean(p_spect, axis=self.subject_data.get_axis_num('event'))
        peaks = par_find_peaks_by_chan(mean_p_spect, self.freqs)

        # now that we know at which each electrode has peaks, compute clusters of electrodes that exhibit peaks at
        # similar frequencies and are close enough together
        self.res['clusters'] = self.find_clusters_from_peaks(peaks, near_adj_matr, allowed_elecs)

    def find_clusters_from_peaks(self, peaks, near_adj_matr, allowed_elecs):
        """
        Given a a frequency by channel array, use the tarjan algorithm to identify clusters of electrodes.

        Parameters
        ----------
        peaks: numpy.ndarray
            frequency x channel boolean array
        near_adj_matr: numpy.ndarray
            square boolean array indicating whether any two electrodes are considered to be near each other
        allowed_elecs: numpy.array
            boolean array the same length as the number of electrodes, indicating whether an electrode can be included
            or should be automatically excluded

        Returns
        -------
        A multilevel dictionary. The top level has a key representing the frequency at which a cluster was detected.
        Each key links to another dictionary, with the keys:
            elecs: a list of electrode numbers in the cluster
            elec_freqs: the precise frequency of the oscillation detected at each electrode
            mean_freqs: the mean of elec_freqs (for some reason..)
        """

        # compute frequency bins
        window_centers = np.arange(self.freqs[0], self.freqs[-1] + .001, 1)
        windows = [(x - self.cluster_freq_range / 2., x + self.cluster_freq_range / 2.) for x in window_centers]
        window_bins = np.stack([(self.freqs >= x[0]) & (self.freqs <= x[1]) for x in windows], axis=0)

        # create dictionary that will hold results
        all_clusters = {k: {'elecs': [], 'mean_freqs': [], 'elec_freqs': []} for k in window_centers}

        # make sure only electrodes of allowed types are included
        peaks[:, ~allowed_elecs] = False

        # bin peaks, count them up, and find the peaks (of the peaks...)
        binned_peaks = np.stack([np.any(peaks[x], axis=0) for x in window_bins], axis=0)
        peak_freqs = my_local_max(binned_peaks.sum(axis=1))

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

                # store all electrodes in the cluster
                all_clusters[window_centers[this_peak_freq]]['elecs'].append(clusters[good_cluster])

                # find mean frequency of cluster, first taking the mean freq within each electrode and then across
                mean_freqs = []
                for elec in peaks[window_bins[this_peak_freq]][:, clusters[good_cluster]].T:
                    mean_freqs.append(np.mean(self.freqs[window_bins[this_peak_freq]][elec]))
                all_clusters[window_centers[this_peak_freq]]['elec_freqs'].append(mean_freqs)
                all_clusters[window_centers[this_peak_freq]]['mean_freqs'].append(np.mean(mean_freqs))

        # return only the dictionary entries that are filled in (ie, only frequencies with clusters)
        return dict((k, v) for k, v in all_clusters.items() if all_clusters[k]['elecs'])

    def _get_elec_xyz(self, col_str='ind.'):
        xyz = self.elec_info[['{}{}'.format(col_str, coord) for coord in ['x', 'y', 'z']]].values
        return xyz

    # automatically set the .res_str based on the class attributes
    @property
    def min_elec_dist(self):
        return self._min_elec_dist

    @min_elec_dist.setter
    def min_elec_dist(self, t):
        self._min_elec_dist = t
        self.set_res_str()

    @property
    def elec_types_allowed(self):
        return self._elec_types_allowed

    @elec_types_allowed.setter
    def elec_types_allowed(self, t):
        self._elec_types_allowed = t
        self.set_res_str()

    @property
    def min_num_elecs(self):
        return self._min_num_elecs

    @min_num_elecs.setter
    def min_num_elecs(self, t):
        self._min_num_elecs = t
        self.set_res_str()

    @property
    def separate_hemis(self):
        return self._separate_hemis

    @separate_hemis.setter
    def separate_hemis(self, t):
        self._separate_hemis = t
        self.set_res_str()

    @property
    def cluster_freq_range(self):
        return self._cluster_freq_range

    @cluster_freq_range.setter
    def cluster_freq_range(self, t):
        self._cluster_freq_range = t
        self.set_res_str()

    def set_res_str(self):
        if np.all([hasattr(self, x) for x in SubjectOscillationClusterAnalysis.attrs_in_res_str]):
            self.res_str = SubjectOscillationClusterAnalysis.res_str_tmp % (self.min_elec_dist,
                                                                            self.min_num_elecs,
                                                                            '_'.join(self.elec_types_allowed),
                                                                            self.separate_hemis,
                                                                            self.cluster_freq_range)