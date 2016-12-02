import os
import pdb
import matplotlib
import ram_data_helpers
import cPickle as pickle
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from scipy.stats.mstats import zscore, zmap
from scipy.stats import binned_statistic, sem, ttest_1samp, ttest_ind
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score, roc_curve
from TH_load_features import load_features
from xray import concat

# import warnings
# warnings.filterwarnings('error')

"""

Data class. handles io and computing features
SubjectClassifier class. does classification. also forward model/univariate stats
Results class. hold results and also plotting

how to put it all together. Make an analysis class.

"""


class SubjectData(object):
    """
    Data class contains default data settings and handles raw(ish) data IO.
    """

    def __init__(self):
        self.task = 'RAM_TH1'
        self.subj = None
        self.feat_phase = ['enc']
        self.feat_type = 'power'
        self.start_time = [-1.2]
        self.end_time = [0.5]
        self.bipolar = True
        self.freqs = np.logspace(np.log10(1), np.log10(200), 8)
        self.hilbert_phase_band = None
        self.freq_bands = None
        self.mean_pow = False
        self.num_phase_bins = None
        self.time_bins = None
        self.ROIs = None
        self.pool = None

        # this will hold the subject data after load_data() is called
        self.subject_data = None

        # if data already exists on disk, just load it. If False, will recompute
        self.load_data_if_file_exists = True

        # base directory to save data
        self.base_dir = '/scratch/jfm2/python'

        # location of save data will be defined after save() is called
        self.save_dir = None
        self.save_file = None

    def load_data(self):
        """
        Loads features for each feature type in self.feat_phase and concats along events dimension.
        """
        if self.subj is None:
            print('Attribute subj must be set before loading data.')
            return

        # define the location where data will be saved and load if already exists and not recomputing
        self.save_dir = self._generate_save_path(self.base_dir)
        self.save_file = os.path.join(self.save_dir, self.subj + '_features.p')
        if self.load_data_if_file_exists & os.path.exists(self.save_file):
            print('Feature data already exists for %s, loading.' % self.subj)
            with open(self.save_file, 'rb') as f:
                subj_features = pickle.load(f)

        # otherwise compute
        else:
            subj_features = []
            # loop over all task phases
            for s_time, e_time, phase in zip(self.start_time if isinstance(self.start_time, list) else [self.start_time],
                                             self.end_time if isinstance(self.end_time, list) else [self.end_time],
                                             self.feat_phase):
                subj_features.append(load_features(self.subj, self.task, phase, s_time, e_time,
                                                   self.time_bins, self.freqs, self.freq_bands,
                                                   self.hilbert_phase_band, self.num_phase_bins, self.bipolar,
                                                   self.feat_type, self.mean_pow, False, '',
                                                   self.ROIs, self.pool))
            if len(subj_features) > 1:
                subj_features = concat(subj_features, dim='events')
            else:
                subj_features = subj_features[0]

        # make sure the events dimension is the first dimension
        if subj_features.dims[0] != 'events':
            ev_dim = np.where(np.array(subj_features.dims) == 'events')[0]
            new_dim_order = np.hstack([ev_dim, np.setdiff1d(range(subj_features.ndim), ev_dim)])
            subj_features = subj_features.transpose(*np.array(subj_features.dims)[new_dim_order])

        # store as self.data
        self.subject_data = subj_features

    def save_data(self):
        """
        Saves self.data as a pickle to location defined by _generate_save_path.
        """
        if self.subject_data is None:
            print('Data must be loaded before saving. Use .load_data()')
            return

        # make directories if missing
        if not os.path.exists(os.path.split(self.save_dir)[0]):
            try:
                os.makedirs(os.path.split(self.save_dir)[0])
            except OSError:
                pass
        if not os.path.exists(self.save_dir):
            try:
                os.makedirs(self.save_dir)
            except OSError:
                pass

        # pickle file
        with open(self.save_file, 'wb') as f:
            pickle.dump(self.subject_data, f, protocol=-1)

    def _generate_save_path(self, base_dir):
        """
        Define save directory based on settings so things stay reasonably organized on disk. Return string.
        """

        f1 = self.freqs[0]
        f2 = self.freqs[-1]
        bipol_str = 'bipol' if self.bipolar else 'mono'
        tbin_str = '1_bin' if self.time_bins is None else str(self.time_bins.shape[0]) + '_bins'

        start_stop_zip = zip(self.feat_phase,
                             self.start_time if isinstance(self.start_time, list) else [self.start_time],
                             self.end_time if isinstance(self.end_time, list) else [self.end_time])
        start_stop_str = '_'.join(['%s_start_%.1f_stop_%.1f' % (x[0], x[1], x[2]) for x in start_stop_zip])

        base_dir = os.path.join(base_dir,
                                self.task,
                                '%d_freqs_%.1f_%.1f_%s' % (len(self.freqs), f1, f2, bipol_str),
                                start_stop_str,
                                tbin_str,
                                self.subj,
                                self.feat_type)
        return base_dir


class SubjectClassifier(SubjectData):
    """
    Subclass of data with methods to handle classification. Some options are specific to the Treasure Hunt (TH) task.
    """

    # class attribute: default regularization value. Event if self.C is modified, this will be used for subjects with
    # only one session of data
    default_C = [7.2e-4]

    def __init__(self):
        super(SubjectClassifier, self).__init__()
        self.train_phase = ['enc']  # ['enc'] or ['rec'] or ['enc', 'rec']
        self.test_phase = ['enc']   # ['enc'] or ['rec'] or ['enc', 'rec'] # PUT A CHECK ON THIS and others, properties?
        self.norm = 'l2'            # type of regularization (l1 or l2)
        self.C = SubjectClassifier.default_C
        self.scale_enc = None
        self.recall_filter_func = ram_data_helpers.filter_events_to_recalled
        self.exclude_by_rec_time = False
        self.rec_thresh = None
        self.load_class_res_if_file_exists = True
        self.save_class = True

        # will hold cross validation fold info after call to make_cross_val_labels(), task_phase will be an array with
        # either 'enc' or 'rec' for each entry in our data
        self.cross_val_dict = {}
        self.task_phase = None

        # will hold classifier results after loaded or computed
        self.class_res = None

        # location to save or load classification results will be defined after call to make_class_dir()
        self.class_save_dir = None
        self.class_save_file = None

    def run_classifier(self):
        """
        Basically a convenience function to do all the classification steps sequentially.
        """
        if self.subject_data is None:
            print('Data must be loaded before running classifier. Use .load_data()')
            return

        # Step 1: create (if needed) directory to save/load
        self.make_class_dir()

        # Step 2: if we want to load results instead of computing, try to load
        if self.load_class_res_if_file_exists:
            self.load_class_data()

        # Step 3: if not loaded ...
        if self.class_res is None:

            # Step 3A: make cross val labels before doing the actual classification
            self.make_cross_val_labels()

            # Step 3B: classify. how to handle normalization in particular the different phases. Do the different
            # phases need to be normalized relative to themselves, or can they be lumped together. together is easier..
            self.classify()

    def make_class_dir(self):
        """
        Create directory where classifier data will be saved/loaded if it needs to be created. This also will define
        self.class_save_dir and self.class_save_file
        """

        self.class_save_dir = self._generate_class_save_path()
        self.class_save_file = os.path.join(self.class_save_dir, self.subj + '_' + self.feat_type + '.p')
        if not os.path.exists(self.class_save_dir):
            try:
                os.makedirs(self.class_save_dir)
            except OSError:
                pass

    def load_class_data(self):
        """
        Load classifier results if they exist and modify self.class_res to hold them.
        """
        if os.path.exists(self.class_save_file):
            with open(self.class_save_file, 'rb') as f:
                class_res = pickle.load(f)
            self.class_res = class_res
        else:
            print('Cannot load %s, does not exist.' % self.class_save_file)

    def make_cross_val_labels(self):
        """
        Creates the training and test folds. If a subject has multiple sessions of data, this will do leave-one-session-
        out cross validation. If only one session, this will do leave-one-list-out CV. Training data will only include
        experiment phases included in self.train_phase and test data will only include phases in self.test_phase.

        Format of .. is ..
        """
        if self.subject_data is None:
            print('Data must be loaded before computing cross validation labels. Use .load_data()')
            return

        # create folds based on either sessions or lists within a session
        sessions = self.subject_data.events.data['session']
        if len(np.unique(self.subject_data.events.data['session'])) > 1:
            folds = sessions
        else:
            trial_str = 'trial' if self.task == 'RAM_TH1' else 'list'
            folds = self.subject_data.events.data[trial_str]

        # The classifier can train and test on different phases of our experiments, namely encoding and retrieval
        # (or both). These are coded different depending on the experiment.
        task_phase = self.subject_data.events.data['type']
        enc_str = 'CHEST' if 'RAM_TH' in self.task else 'WORD'
        rec_str = 'REC' if 'RAM_TH' in self.task else 'REC_WORD'
        task_phase[task_phase == enc_str] = 'enc'
        task_phase[task_phase == rec_str] = 'rec'
        valid_train_inds = np.array([True if x in self.test_phase else False for x in task_phase])
        valid_test_inds = np.array([True if x in self.test_phase else False for x in task_phase])

        # make dictionary to hold booleans for training and test indices for each fold, as well as the task phase for
        #  each fold
        cv_dict = {}
        uniq_folds = np.unique(folds)
        for fold in uniq_folds:
            cv_dict[fold] = {}
            cv_dict[fold]['train_bool'] = (folds != fold) & valid_train_inds
            cv_dict[fold]['test_bool'] = (folds == fold) & valid_test_inds
            cv_dict[fold]['train_phase'] = task_phase[(folds != fold) & valid_train_inds]
            cv_dict[fold]['test_phase'] = task_phase[(folds == fold) & valid_test_inds]
        self.cross_val_dict = cv_dict
        self.task_phase = task_phase

    def classify(self):
        """
        Does the actual classification. I wish I could simplify this a bit, but, it's got a lot of steps to do!
        """
        if not self.cross_val_dict:
            print('Cross validation labels must be computed before running classifier. Use .make_cross_val_labels()')
            return

        # The bias correct only works for subjects with multiple sessions ofdata, see comment below.
        if (len(np.unique(self.subject_data.events.data['session'])) == 1) & (len(self.C) > 1):
            print('Multiple C values cannot be tested for a subject with only one session of data.')
            return

        # Get class labels
        Y = self.recall_filter_func(self.task, self.subject_data.events.data, self.rec_thresh)

        # reshape data to events x number of features
        X = self.subject_data.data.reshape(self.subject_data.shape[0], -1)

        # normalize data by session if the features are oscillatory power
        if self.feat_type == 'power':
            X = self._normalize_power(X)

        # revert C value to default C value not multi session subejct
        Cs = self.C
        loso = True
        if len(np.unique(self.subject_data.events.data['session'])) == 1:
            Cs = ClassifyTH.default_C
            loso = False

        # if leave-one-session-out (loso) cross validation, this will hold area under the curve for each hold out
        fold_aucs = np.empty(shape=(len(self.cross_val_dict.keys()), len(self.C)), dtype=np.float)

        # will hold the predicted class probability for all the test data
        probs = np.empty(shape=(Y.shape[0], len(Cs)), dtype=np.float)

        # This outer loop is for all the different penalty (C) values given. If more than one, the optimal value will
        # be chosen using bias correction based on Tibshirani and Tibshirani 2009, Annals of Applied Statistics. This
        # only works for multi-session (loso) data, otherwise we don't have enough data to compute help out AUC.
        for c_num, c in enumerate(Cs):
            
            # create classifier with current C
            lr_classifier = LogisticRegression(C=c, penalty=self.norm, solver='liblinear')

            # now loop over all the cross validation folds
            for cv_num, cv in enumerate(self.cross_val_dict.keys()):

                # Training data for fold
                x_train = X[self.cross_val_dict[cv]['train_bool']]
                task_train = self.cross_val_dict[cv]['train_phase']
                y_train = Y[self.cross_val_dict[cv]['train_bool']]

                # Test data for fold
                x_test = X[self.cross_val_dict[cv]['test_bool']]
                task_test = self.cross_val_dict[cv]['test_phase']
                y_test = Y[self.cross_val_dict[cv]['test_bool']]

                # normalize train test and scale test data. This is ugly because it has to account for a few
                # different contingencies. If the test data phase is also a train data phase, then scale test data for
                # that phase based on the training data for that phase. Otherwise, just zscore the test data.
                for phase in self.train_phase:
                    x_train[task_train == phase] = zscore(x_train[task_train == phase], axis=0)

                for phase in self.test_phase:
                    if phase in self.train_phase:
                        x_test[task_test == phase] = zmap(x_test[task_test == phase],
                                                          x_train[task_train == phase], axis=0)
                    else:
                        x_test[task_test == phase] = zscore(x_test[task_test == phase], axis=0)

                # weight observations by number of positive and negative class
                y_ind = y_train.astype(int)

                # if we are training on both encoding and retrieval and we are scaling the encoding weights,
                # seperate the ecnoding and retrieval positive and negative classes so we can scale them later
                if len(self.train_phase) > 1 and (self.scale_enc is not None):
                    y_ind[task_train == 'rec'] += 2

                # compute the weight vector as the reciprocal of the number of items in each class, divided by the mean
                # class frequency
                recip_freq = 1. / np.bincount(y_ind)
                recip_freq /= np.mean(recip_freq)

                # scale the encoding classes. Sorry for the identical if statements
                if len(self.train_phase) > 1 and (self.scale_enc is not None):
                    recip_freq[:2] *= self.scale_enc
                    recip_freq /= np.mean(recip_freq)
                weights = recip_freq[y_ind]

                # Fit the model
                lr_classifier.fit(x_train, y_train, sample_weight=weights)

                # now predict class probability of test data
                test_probs = lr_classifier.predict_proba(x_test)[:, 1]
                probs[self.cross_val_dict[cv]['test_bool'], c_num] = test_probs
                if loso:
                    fold_aucs[cv_num, c_num] = roc_auc_score(y_test, test_probs)

            # If multi sessions, compute bias corrected AUC (if only one C values given, this has no effect) because
            # the bias will be 0. AUC is the average AUC across folds for the the bees value of C minus the bias term.
            if loso:
                aucs = fold_aucs.mean(axis=0)
                bias = np.mean(fold_aucs.max(axis=1) - fold_aucs[:, np.argmax(aucs)])
                auc = aucs.max() - bias
            else:
                # is not multi session, AUC is just computed by aggregating all the hold out probabilities
                test_bool = np.any(np.stack([self.cross_val_dict[x]['test_bool'] for x in self.cross_val_dict]), axis=0)
                auc = roc_auc_score(Y[test_bool], probs[test_bool])
            pdb.set_trace()

    # # output results, including AUC, lr object (fit to all data), prob estimates, and class labels
    # subj_res = {}
    # subj_res['auc'] = auc
    # # print subj, aucs.max(), aucs.max() - bias, self.C[np.argmax(aucs)]
    # subj_res['subj'] = subj
    #
    # lr_classifier.C = self.C[np.argmax(aucs)]
    # subj_res['lr_classifier'] = lr_classifier.fit(feat_mat, recalls)
    # # subj_res['lr_classifier'] = lr2.fit(feat_mat, recalls)
    # # print subj, lr2.C_
    # subj_res['probs'] = probs[test_bool, np.argmax(aucs)]

    def _normalize_power(self, X):
        """
        Normalizes (zscores) each column in X. If rows of comprised of different task phases, each task phase is
        normalized to itself

        returns normalized X
        """
        uniq_sessions = np.unique(self.subject_data.events.data['session'])
        for sess in uniq_sessions:
            sess_event_mask = (self.subject_data.events.data['session'] == sess)
            for phase in set(self.train_phase + self.test_phase):
                task_mask = self.task_phase == phase
                X[sess_event_mask & task_mask] = zscore(X[sess_event_mask & task_mask], axis=0)
        return X

    def _generate_class_save_path(self):
        """
        Build path to where classification results should be saved (or loaded from). Return string.
        """

        if len(self.C) > 1:
            dir_str = 'C_bias_norm_%s_%s_train_%s_test_%s'
            dir_str_vals = (
                self.norm,
                self.recall_filter_func.__name__,
                '_'.join(self.train_phase),
                '_'.join(self.test_phase))
        else:
            dir_str = 'C_%.8f_norm_%s_%s_train_%s_test_%s'
            dir_str_vals = (
                self.C[0],
                self.norm,
                self.recall_filter_func.__name__,
                '_'.join(self.train_phase),
                '_'.join(self.test_phase))

        return os.path.join(os.path.split(self.save_dir)[0], dir_str % dir_str_vals)








class ClassifyTH:

    # some hardcoded settings
    base_dir = '/scratch/jfm2/python/'
    default_C = [7.2e-4]

    def __init__(self, **kwargs):

        # dictionary of default settings for classification
        prop_defaults = {
            'subjs': None,
            'task': 'RAM_TH1',
            'train_phase': 'enc',   # task phase(s) to train on
            'test_phase': 'enc',    # task phase(s) to test fit model on
            'feat_phase': ['enc'],  # task phases to compute features. train_phase and test_phase must be in feat_phase
            'start_time': -1.2,     # start and end time(s) to use. Enter in the same order corresponding to feat_phase
            'end_time': 0.5,
            'bipolar': True,        # monopolar (average re-reference) or bipolar
            'freqs': np.logspace(np.log10(1), np.log10(200), 8),  # Frequencies where we will compute power
            'freq_bands': None,     # these following settings apply to the 'pow_by_phase' feature type
            'hilbert_phase_band': None,
            'num_phase_bins': None,
            'time_bins': None,      # An array of time bins to average. If None, will  average from start to end_time
            'norm': 'l2',           # type of regularization (l1 or l2)
            'feat_type': 'power',   # features to use for regression
            'do_rand': False,
            'scale_enc': None,
            'C': ClassifyTH.default_C,  # penalty parameter (1/lamda)
            'ROIs': None,                 # to restrict to just specific ROIs, enter a list of ROIs
            'compute_pval': False,
            'recall_filter_func': ram_data_helpers.filter_events_to_recalled,
            'exclude_by_rec_time': False,
            'rec_thresh': None,
            'force_reclass': False,
            'save_class': True,
            'pool': None
        }

        # make sure any user input arguments are valid possibilities
        input_check = False
        for key in kwargs.keys():
            if key not in prop_defaults:
                input_check = True
                print('Invalid argument %s' % key)
        if input_check:
            return

        # set all attributes
        for (prop, default) in prop_defaults.iteritems():
            setattr(self, prop, kwargs.get(prop, default))

        # make sure the task phases entered make sense
        self.train_phase = self.train_phase if isinstance(self.train_phase, list) else [self.train_phase]
        self.test_phase = self.test_phase if isinstance(self.test_phase, list) else [self.test_phase]
        self.feat_phase = self.feat_phase if isinstance(self.feat_phase, list) else [self.feat_phase]
        if not set(self.train_phase + self.test_phase).issubset(self.feat_phase):
            print('train_phase and test_phase must be a subset of feat_phase')
            return

        # if subjects not given, get list from /data/events/ directory
        if self.subjs is None:
            self.subjs = ram_data_helpers.get_subjs(self.task)

        # this is stupid but don't let it precess some subjects. R1132C didn't use the confident judgements so we can't
        # work with the data.
        if self.task == 'RAM_TH1':
            self.subjs = [subj for subj in self.subjs if subj != 'R1132C']
            self.subjs = [subj for subj in self.subjs if subj != 'R1219C']
            self.subjs = [subj for subj in self.subjs if subj != 'R1227T']

        # input to load features telling it whether to average the values after computing
        self.mean_pow = False if self.feat_type == 'pow_by_phase' else True

        # where to save data
        self.base_dir = os.path.join(ClassifyTH.base_dir, self.task)

        # holds the output from all subjects
        self.res = None
        self.summary_table = None

    def run_classify_for_all_subjs(self):
        """

        :return:
        """
        class_data_all_subjs = []
        for subj in self.subjs:
            print 'Processing %s.' % subj

            # define base directory for subject. Name the directory based on a variety of parameters so things stay
            # reasonably organized on disk
            f1 = self.freqs[0]
            f2 = self.freqs[-1]
            bipol_str = 'bipol' if self.bipolar else 'mono'
            tbin_str = '1_bin' if self.time_bins is None else str(self.time_bins.shape[0])+'_bins'

            start_stop_zip = zip(self.feat_phase,
                                 self.start_time if isinstance(self.start_time, list) else [self.start_time],
                                 self.end_time if isinstance(self.end_time, list) else [self.end_time])
            start_stop_str = '_'.join(['%s_start_%.1f_stop_%.1f' % (x[0], x[1], x[2]) for x in start_stop_zip])

            subj_base_dir = os.path.join(self.base_dir,
                                         '%d_freqs_%.1f_%.1f_%s' % (len(self.freqs), f1, f2, bipol_str),
                                         start_stop_str,
                                         tbin_str,
                                         subj)

            # if self.train_phase != 'both':
            #     subj_base_dir = os.path.join(self.base_dir, '%d_freqs_%.1f_%.1f_%s' % (len(self.freqs), f1, f2, bipol_str),
            #                                  '%s_start_%.1f_stop_%.1f' % (self.train_phase, self.start_time, self.end_time),
            #                                  tbin_str, subj)
            # else:
            #     subj_base_dir = os.path.join(self.base_dir, '%d_freqs_%.1f_%.1f_%s' % (len(self.freqs), f1, f2, bipol_str),
            #                                  'enc_start_%.1f_stop_%.1f_rec_%.1f_stop_%.1f' %
            #                                  (self.start_time[0], self.end_time[0], self.start_time[1], self.end_time[1]),
            #                                  tbin_str, subj)

            # sub directories hold electrode data, feature data, and classifier output
            subj_elec_dir = os.path.join(subj_base_dir, 'elec_data')
            subj_feature_dir = os.path.join(subj_base_dir, '%s' % self.feat_type)
            subj_class_dir = os.path.join(subj_base_dir, 'C_bias_%s_%s_train_%s_test_%s' %
                                          (self.norm,
                                           self.recall_filter_func.__name__,
                                           '_'.join(self.train_phase),
                                           '_'.join(self.test_phase)))

            # this holds the classifier results
            save_file = os.path.join(subj_class_dir, subj + '_' + self.feat_type + '.p')

            # features used for classification
            feat_file = os.path.join(subj_feature_dir, subj + '_features.p')

            # make sure we can even load the events
            try:
                events = ram_data_helpers.load_subj_events(self.task, subj, self.feat_phase)
            except (ValueError, AttributeError, IOError):
                print 'Error processing %s. Could not load events.' % subj

            # If features exist, we will not overwrite them unless the number of events has changes (ie., a new session
            # was run)
            overwrite_features = False

            # If the classifier file already exists, load it. Again, only rerun if number of events has changed.
            if os.path.exists(save_file):
                with open(save_file, 'rb') as f:
                    subj_data = pickle.load(f)
                if len(events) == len(subj_data['events']):
                    if not self.force_reclass:
                        print 'SubjectClassifier exists for %s. Skipping.' % subj
                        class_data_all_subjs.append(subj_data)
                        self.res = class_data_all_subjs
                        continue
                else:
                    overwrite_features = True

            # Check the features vs the current events file
            if os.path.exists(feat_file):
                with open(feat_file, 'rb') as f:
                    feat_data = pickle.load(f)
                if len(events) != len(feat_data['events']):
                    overwrite_features = True

                # this is stupid, but this subject's events on disk don't align with features because some of the events
                # are too close the edge of the eeg file
                if subj in ['R1208C', 'R1201P_1']:
                    overwrite_features = False

            # make directories if missing
            if (not os.path.exists(subj_class_dir)) or (not os.path.exists(subj_feature_dir)):
                try:
                    os.makedirs(subj_class_dir)
                except OSError:
                    pass
                try:
                    os.makedirs(subj_feature_dir)
                except OSError:
                    pass
                try:
                    os.makedirs(subj_elec_dir)
                except OSError:
                    pass

            # run classifier for subject
            # try:
            subj_classify = self.run_classify_for_single_subj(subj, feat_file,
                                                              subj_elec_dir, overwrite_features)
            class_data_all_subjs.append(subj_classify)
            if self.save_class:
                with open(save_file, 'wb') as f:
                    pickle.dump(subj_classify, f, protocol=-1)
            # except:
            #   print 'Error processing %s.' % subj
            self.res = class_data_all_subjs

            # summarize results in dataframe. mean err, median err, auc, cv, low conf %
            data = np.array([[np.mean(x['events'].data['distErr']), np.median(x['events'].data['distErr']),
                              x['auc'], x['cv_type'] == 'loso', np.mean(x['events'].data['confidence'] == 0),
                              x['lr_classifier'].C] for x in self.res])
            subjs = [x['subj'] for x in self.res]
            self.summary_table = pd.DataFrame(data=data, index=subjs,
                                              columns=['MeanErr', 'MedErr', 'AUC', 'LOSO', 'LowConf', 'C'])


    def run_classify_for_single_subj(self, subj, feat_file, subj_elec_dir, overwrite_features):
        """
        Runs logistic regression classifier to predict recalled/not-recalled items. Logic is as follows:
        1) Create classifier features, if they do not exist.
        2) Classify using leave one session out cross validation. If only one session, use leave one list out
        """

        # check if features exist. If not, or we are overwriting, create.
        if not os.path.exists(feat_file) or overwrite_features or self.feat_type[-2:] == '_r':
            print '%s features do not exist or overwriting for %s. Creating.' % (self.feat_type, subj)

            subj_features = []
            for s_time, e_time, phase in zip(self.start_time if isinstance(self.start_time, list) else [self.start_time],
                                             self.end_time if isinstance(self.end_time, list) else [self.end_time],
                                             self.feat_phase):
                subj_features.append(load_features(subj, self.task, phase, s_time, e_time,
                                                   self.time_bins, self.freqs, self.freq_bands,
                                                   self.hilbert_phase_band, self.num_phase_bins, self.bipolar,
                                                   self.feat_type, self.mean_pow, False, subj_elec_dir,
                                                   self.ROIs, self.pool))
            if len(subj_features) > 1:
                subj_features = concat(subj_features, dim='events')
            else:
                subj_features = subj_features[0]

            # save features to disk
            with open(feat_file, 'wb') as f:
                pickle.dump(subj_features, f, protocol=-1)

        # if exist, load from disk
        else:
            with open(feat_file, 'rb') as f:
                subj_features = pickle.load(f)

        # determine classes
        recalls = self.recall_filter_func(self.task, subj_features.events.data, self.rec_thresh)

        # give encoding and retrieval events a common name regardless of task
        task_phase = subj_features.events.data['type']
        if self.task == 'RAM_TH1':
            task_phase[task_phase == 'CHEST'] = 'enc'
            task_phase[task_phase == 'REC'] = 'rec'
        elif self.task == 'RAM_FR1':
            task_phase[task_phase == 'WORD'] = 'enc'
            task_phase[task_phase == 'REC_WORD'] = 'rec'

        # create cross validation labels
        sessions = subj_features.events.data['session']
        if len(np.unique(subj_features.events.data['session'])) > 1:
            cv_sel = sessions
        else:
            trial_str = 'trial' if self.task == 'RAM_TH1' else 'list'
            cv_sel = subj_features.events.data[trial_str]

        # transpose to have events as first dimensions
        if (self.feat_type == 'pow_by_phase') | (self.time_bins is not None):
            dims = subj_features.dims
            subj_features = subj_features.transpose(dims[2], dims[1], dims[0], dims[3])
        else:
            subj_features = subj_features.transpose()
        subj_data = subj_features.data.reshape(subj_features.data.shape[0], -1)

        good_ev = np.ones(subj_features.events.data.shape, dtype=bool)
        if self.exclude_by_rec_time:
            inds = task_phase == 'rec'
            if 'rec' in self.feat_phase:
                good_ev[inds & (subj_features.events.data['move_rt'] < self.end_time)] = False
            elif 'rec_circle' in self.feat_phase:
                rt = subj_features.events.data['reactionTime'] - subj_features.events.data['choice_rt']
                good_ev[inds & (rt < np.abs(self.start_time))] = False

        # run classification
        subj_res = self.compute_classifier(subj, recalls[good_ev], sessions[good_ev], cv_sel[good_ev],
                                           subj_data[good_ev], task_phase[good_ev])

        if self.compute_pval:
            aucs = np.empty(shape=(200, 1), dtype=np.float64)
            recalls_rand = np.copy(recalls)

            for i in range(aucs.shape[0]):
                np.random.shuffle(recalls_rand)
                res_tmp = self.compute_classifier(subj, recalls_rand, sessions, cv_sel, subj_data, task_phase)
                aucs[i] = res_tmp['auc']
            subj_res['p_val'] = np.mean(subj_res['auc'] < aucs)

        # add some extra info to the res dictionary
        subj_res['subj'] = subj
        subj_res['events'] = subj_features['events']
        print subj_res['auc']
        if np.any(np.array(['power', 'pow_by_phase']) == self.feat_type):
            subj_res['loc_tag'] = subj_features.attrs['loc_tag']
            subj_res['anat_region'] = subj_features.attrs['anat_region']
            subj_res['chan_tags'] = subj_features.attrs['chan_tags']
            subj_res['channels'] = subj_features.channels.data if not self.bipolar else subj_features.bipolar_pairs.data

        return subj_res

    def compute_classifier(self, subj, recalls, sessions, cv_sel, feat_mat, task_phase):

        train_phase = ['rec' if 'rec' in x else 'enc' for x in self.train_phase]
        train_bool = np.array([True if x in train_phase else False for x in task_phase])
        test_phase = ['rec' if 'rec' in x else 'enc' for x in self.test_phase]
        test_bool = np.array([True if x in test_phase else False for x in task_phase])

        if self.do_rand:
            feat_mat = np.random.rand(*feat_mat.shape)

        # normalize data by session (this only makes sense for power?)
        if self.feat_type == 'power':
            uniq_sessions = np.unique(sessions)
            for sess in uniq_sessions:
                sess_event_mask = (sessions == sess)
                for phase in set(train_phase + test_phase):
                    task_mask = task_phase == phase
                    feat_mat[sess_event_mask & task_mask] = zscore(feat_mat[sess_event_mask & task_mask], axis=0)

        # loop over each train set and classify test
        uniq_cv = np.unique(cv_sel)
        fold_aucs = np.empty(shape=(len(uniq_cv), len(self.C)), dtype=np.float)
        aucs = np.zeros(shape=(len(self.C)), dtype=np.float)

        probs = np.empty(shape=(recalls.shape[0], len(self.C)), dtype=np.float)

        Cs = self.C
        loso = True
        if len(np.unique(sessions)) == 1:
            Cs = ClassifyTH.default_C
            loso = False

        for c_num, c in enumerate(Cs):
            lr_classifier = LogisticRegression(C=c, penalty=self.norm, solver='liblinear')

            for cv_num, cv in enumerate(uniq_cv):

                # create mask of just training data
                mask_cv = (cv_sel != cv)
                mask_train = mask_cv & train_bool
                feats_train = feat_mat[mask_train]
                task_phase_train = task_phase[mask_train]
                rec_train = recalls[mask_train]

                # mask test data
                mask_test = ~mask_cv & test_bool
                task_phase_test = task_phase[mask_test]
                feats_test = feat_mat[mask_test]

                # normalize train test and scale test data. This is ugly because it has to account for a few
                # different contingencies. If the test data phase is also a train data phase, then scale test data for
                # that phase based on the training data for that phase. Otherwise, just zscore the test data.
                x_train = np.empty(shape=feats_train.shape)
                for phase in train_phase:
                    x_train[task_phase_train == phase] = zscore(feats_train[task_phase_train == phase], axis=0)

                x_test = np.empty(shape=feats_test.shape)
                for phase in test_phase:
                    if phase in train_phase:
                        x_test[task_phase_train == phase] = zmap(feats_test[task_phase_test == phase],
                                                                 feats_train[task_phase_train == phase], axis=0)
                    else:
                        x_test[task_phase_train == phase] = zscore(feats_test[task_phase_test == phase], axis=0)

                # weight observations by number of positive and negative class
                y_ind = rec_train.astype(int)

                # if we are training on both encoding and retrieval and we are scaling the encoding weights,
                # seperatate the enoding and retrieval positive and negative classes so we can scale them later
                if len(self.train_phase) > 1 and (self.scale_enc is not None):
                    y_ind[task_phase_train == 'rec'] += 2

                # compute the weight vector as the reciprocal of the number of items in each class, divided by the mean
                # class frequency
                recip_freq = 1. / np.bincount(y_ind)
                recip_freq /= np.mean(recip_freq)

                # scale the encoding classes. Sorry for the identical if statements
                if len(self.train_phase) > 1 and (self.scale_enc is not None):
                    recip_freq[:2] *= self.scale_enc
                    recip_freq /= np.mean(recip_freq)
                weights = recip_freq[y_ind]

                # lr_classifier = LogisticRegression(C=c*x_train.shape[0], penalty=self.norm, solver='liblinear')
                lr_classifier.fit(x_train, rec_train, sample_weight=weights)

                # now estimate classes of train data
                test_probs = lr_classifier.predict_proba(x_test)[:, 1]
                probs[mask_test, c_num] = test_probs
                if loso:
                    fold_aucs[cv_num, c_num] = roc_auc_score(recalls[mask_test], test_probs)

            # compute AUC
            if loso:
                aucs = fold_aucs.mean(axis=0)
                bias = np.mean(fold_aucs.max(axis=1) - fold_aucs[:, np.argmax(aucs)])
                auc = aucs.max() - bias
            else:
                auc = roc_auc_score(recalls[test_bool], probs[test_bool, c_num])

        # output results, including AUC, lr object (fit to all data), prob estimates, and class labels
        subj_res = {}
        subj_res['auc'] = auc
        # print subj, aucs.max(), aucs.max() - bias, self.C[np.argmax(aucs)]
        subj_res['subj'] = subj

        lr_classifier.C = self.C[np.argmax(aucs)]
        subj_res['lr_classifier'] = lr_classifier.fit(feat_mat, recalls)
        # subj_res['lr_classifier'] = lr2.fit(feat_mat, recalls)
        # print subj, lr2.C_
        subj_res['probs'] = probs[test_bool, np.argmax(aucs)]
        subj_res['classes'] = recalls[test_bool]
        subj_res['tercile'] = self.compute_terciles(subj_res)
        subj_res['sessions'] = sessions
        subj_res['cv_sel'] = cv_sel
        subj_res['task_phase'] = task_phase
        subj_res['cv_type'] = 'loso' if len(np.unique(sessions)) > 1 else 'lolo'

        # also compute forward model feature importance for each electrode if we are using power
        # if False:
        if (self.feat_type in ['power', 'pow_by_phase']) and self.time_bins is None:
            probs_log = np.log(subj_res['probs'] / (1 - subj_res['probs']))
            covx = np.cov(feat_mat.T)
            covs = np.cov(probs_log)
            W = subj_res['lr_classifier'].coef_
            A = np.dot(covx, W.T) / covs
            ts, ps = ttest_ind(feat_mat[recalls], feat_mat[~recalls])
            if self.feat_type == 'power':
                subj_res['forward_model'] = np.reshape(A, (-1, len(self.freqs)))
                subj_res['univar_ts'] = np.reshape(ts, (-1, len(self.freqs)))
            else:
                subj_res['forward_model'] = np.reshape(A, (-1, self.freq_bands.shape[0]*self.num_phase_bins))
                subj_res['univar_ts'] = np.reshape(ts, (-1, self.freq_bands.shape[0]*self.num_phase_bins))
        return subj_res

    def plot_terciles(self, subjs=None, cv_type=('loso', 'lolo')):
        if subjs is None:
            subjs = self.subjs

        # get terciles from specified subjects with specifed cv_type
        terciles = np.stack([x['tercile'] for x in self.res if x['cv_type'] in cv_type and x['subj'] in subjs])

        yerr = sem(terciles, axis=0) * 1.96
        plt.bar(range(3), np.nanmean(terciles, axis=0), align='center', color=[.5, .5, .5], linewidth=2, yerr=yerr,
                error_kw={'ecolor': 'k', 'linewidth': 2, 'capsize': 5, 'capthick': 2})

    def plot_auc_hist(self, subjs=None, cv_type=('loso', 'lolo')):
        if subjs is None:
            subjs = self.subjs

        # get aucs from specified subjects with specifed cv_type
        aucs = np.stack([x['auc'] for x in self.res if x['cv_type'] in cv_type and x['subj'] in subjs])

        hist = np.histogram(aucs, np.linspace(0.025, .975, 20))
        plt.bar(np.linspace(0.05, .95, 19), hist[0].astype(np.float) / np.sum(hist[0]), .05, align='center',
                color=[.5, .5, .5], linewidth=2)

    def aucs(self, subjs=None, cv_type=('loso', 'lolo')):
        if subjs is None:
            subjs = self.subjs

        # get terciles from specified subjects with specifed cv_type
        aucs = np.stack([x['auc'] for x in self.res if x['cv_type'] in cv_type and x['subj'] in subjs])
        return aucs

    def mean_auc(self, subjs=None, cv_type=('loso', 'lolo')):
        if subjs is None:
            subjs = self.subjs

        # get terciles from specified subjects with specifed cv_type
        aucs = np.stack([x['auc'] for x in self.res if x['cv_type'] in cv_type and x['subj'] in subjs])
        return aucs.mean()

    def print_res_table(self, subjs=None, cv_type=('loso', 'lolo')):
        if subjs is None:
            subjs = self.subjs
        rows = self.summary_table.index.isin([x == 'loso' for x in cv_type]) & self.summary_table.index.isin(subjs)
        print self.summary_table.loc[rows]

    def compute_feature_heatmap(self, subjs=None, cv_type=('loso', 'lolo'), plot_uni=False, hemi='both',
                                regions=('IFG', 'MFG', 'SFG', 'MTL', 'Hipp', 'TC', 'IPC', 'SPC', 'OC')):
        if subjs is None:
            subjs = self.subjs

        # concatenate all foward model features into one 3d array (freqs x regions x subjs)
        freq_x_regions_x_subjs = []
        region_counts = []
        subjs = np.array(subjs)
        subjs_used = np.zeros(len(subjs), dtype=bool)
        for subj_res in self.res:
            if (subj_res['subj'] in subjs) and (subj_res['cv_type'] in cv_type):
                loc_dict = ram_data_helpers.bin_elec_locs(subj_res['loc_tag'], subj_res['anat_region'],
                                                          subj_res['chan_tags'])
                subjs_used[subjs == subj_res['subj']] = True
                A = subj_res['forward_model']
                if plot_uni:
                    A = subj_res['univar_ts']
                freq_x_regions = np.empty(A.shape[1] * (len(regions)+1)).reshape(A.shape[1], len(regions)+1)
                freq_x_regions[:] = np.nan

                hemi_bool = np.array([True]*len(loc_dict['is_right']))
                if hemi == 'right':
                    hemi_bool = loc_dict['is_right']
                elif hemi == 'left':
                    hemi_bool = ~loc_dict['is_right']

                region_counts_subj = np.zeros(len(regions)+1)
                other_region = np.ones(A.shape[0], dtype=bool)
                for i, r in enumerate(regions):
                    other_region[loc_dict[r]] = False
                    freq_x_regions[:, i] = A[loc_dict[r] & hemi_bool, :].mean(axis=0)
                    region_counts_subj[i] = np.sum(loc_dict[r] & hemi_bool)
                freq_x_regions[:, i+1] = A[other_region & hemi_bool, :].mean(axis=0)
                region_counts_subj[i+1] = np.sum(other_region & hemi_bool)

                freq_x_regions_x_subjs.append(freq_x_regions)
                region_counts.append(region_counts_subj)
        region_counts = np.stack(region_counts, -1)
        feature_heatmap = np.stack(freq_x_regions_x_subjs, -1)
        regions += ('XX',)

        # plot heatmap. If just one subject, plot actual forward model weights. Otherwise, plot t-stat against zero
        if feature_heatmap.shape[2] > 1:
            region_labels = ['%s (%d)' % (x[0], x[1]) for x in zip(regions, region_counts.sum(axis=1))]
            plot_data, p = ttest_1samp(feature_heatmap, 0, axis=2, nan_policy='omit')
            colorbar_str = 't-stat'
        else:
            region_labels = ['%s (%d)' % (x[0], x[1]) for x in zip(regions, region_counts)]
            plot_data = np.nanmean(feature_heatmap, axis=2)
            colorbar_str = 'Feature Importance'

        clim = np.max(np.abs([np.nanmin(plot_data), np.nanmax(plot_data)]))
        fig, ax = plt.subplots(1, 1)
        im = plt.imshow(plot_data, interpolation='nearest', cmap='RdBu_r', vmin=-clim, vmax=clim, aspect='auto')
        cb = plt.colorbar()
        cb.set_label(label=colorbar_str, size=16)  # ,rotation=90)
        cb.ax.tick_params(labelsize=12)
        plt.xticks(range(len(regions)), region_labels, fontsize=16, rotation=-45)
        plt.yticks(range(len(self.freqs))[::3], (np.round(self.freqs * 10) / 10)[::3],
                   fontsize=16)
        plt.ylabel('Frequency', fontsize=16)
        plt.gca().invert_yaxis()
        plt.tight_layout()
        return feature_heatmap, im

    def compute_feature_heatmap_elecs(self, subjs=None, cv_type=('loso', 'lolo'), plot_uni=False, hemi='both',
                                      regions=('IFG', 'MFG', 'SFG', 'MTL', 'Hipp', 'TC', 'IPC', 'SPC', 'OC')):
        if subjs is None:
            subjs = self.subjs

        # concatenate all foward model features into one 3d array (freqs x regions x subjs)
        elecs_by_freqs_by_subjs = []
        region_counts = []
        subjs = np.array(subjs)
        subjs_used = np.zeros(len(subjs), dtype=bool)
        other_tags = []
        for subj_res in self.res:
            if (subj_res['subj'] in subjs) and (subj_res['cv_type'] in cv_type):
                loc_dict = ram_data_helpers.bin_elec_locs(subj_res['loc_tag'], subj_res['anat_region'],
                                                          subj_res['chan_tags'])
                subjs_used[subjs == subj_res['subj']] = True
                A = subj_res['forward_model']
                cb_text = 'Feature Importance'
                if plot_uni:
                    A = subj_res['univar_ts']
                    cb_text = 't-stat'
                # pdb.set_trace()
                freq_x_regions = np.empty(A.shape[1] * len(regions)).reshape(A.shape[1], len(regions))
                freq_x_regions[:] = np.nan

                hemi_bool = np.array([True]*len(loc_dict['is_right']))
                if hemi == 'right':
                    hemi_bool = loc_dict['is_right']
                elif hemi == 'left':
                    hemi_bool = ~loc_dict['is_right']

                region_counts_subj = []
                regions_subj = []
                elecs_by_freqs = []
                clim = np.max(np.abs([np.nanmin(A), np.nanmax(A)]))
                fig, ax = plt.subplots(1, len(regions)+1)
                fig.set_size_inches(18, 6)
                other_region = np.ones(A.shape[0], dtype=bool)
                for i, r in enumerate(regions):
                    if np.any(loc_dict[r] & hemi_bool):
                        other_region[loc_dict[r]] = False
                        im = ax[i].imshow(A[loc_dict[r] & hemi_bool, :].T, interpolation='nearest',
                                          vmin=-clim, vmax=clim, cmap='RdBu_r', aspect='auto')
                        ax[i].invert_yaxis()
                        ax[i].set_xticks(range(np.sum(loc_dict[r] & hemi_bool))[9::10])
                        ax[i].set_xticklabels(ax[i].get_xticks() + 1, fontsize=14)
                    else:
                        ax[i].set_xticks([])
                    if i == 0:
                        ax[i].set_yticks(range(len(self.freqs))[::3])
                        ax[i].set_yticklabels((np.round(self.freqs * 10) / 10)[::3], fontsize=16)
                        ax[i].set_ylabel('Frequency', fontsize=16)
                    else:
                        ax[i].set_yticks([])
                    ax[i].set_title(r)

                if np.any(other_region & hemi_bool):
                    other_tags.append(subj_res['anat_region'][other_region & hemi_bool])
                    im = ax[i + 1].imshow(A[other_region & hemi_bool, :].T, interpolation='nearest',
                                          vmin=-clim, vmax=clim, cmap='RdBu_r', aspect='auto')
                    ax[i + 1].invert_yaxis()
                    ax[i + 1].set_xticks(range(np.sum(other_region & hemi_bool))[9::10])
                    ax[i + 1].set_xticklabels(ax[i+1].get_xticks() + 1, fontsize=14)
                else:
                    ax[i + 1].set_xticks([])
                ax[i + 1].set_yticks([])
                ax[i + 1].set_title('Other')

                fig.subplots_adjust(right=0.93)
                cbar_ax = fig.add_axes([0.95, .125, 0.02, .775])
                cb = fig.colorbar(im, cax=cbar_ax)
                cb.set_label(label=cb_text, size=16)  # ,rotation=90)
                fig.suptitle(subj_res['subj'], fontsize=20)
                # plt.tight_layout()
        return other_tags

    @classmethod
    def compute_terciles(cls, subj_res):
        binned_data = binned_statistic(subj_res['probs'], subj_res['classes'], statistic='mean',
                                       bins=np.percentile(subj_res['probs'], [0, 33, 67, 100]))
        tercile_delta_rec = (binned_data[0] - np.mean(subj_res['classes'])) / np.mean(subj_res['classes']) * 100
        return tercile_delta_rec
