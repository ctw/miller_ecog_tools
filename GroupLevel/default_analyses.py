"""
Contains default settings for various analyses to make it easier to run.
"""
import ram_data_helpers
import numpy as np
from SubjectLevel.Analyses import *


def get_default_analysis_params(analysis='classify_enc', subject_settings='default'):
    """
    Returns a dictionary of parameters for the desired combination of analysis type and subject settings.
    """

    params = {}
    if (analysis == 'classify_enc') or (analysis == 'default'):
        params['ana_class'] = subject_classifier.SubjectClassifier
        params['train_phase'] = ['enc']
        params['test_phase'] = ['enc']
        params['norm'] = 'l2'
        params['recall_filter_func'] = ram_data_helpers.filter_events_to_recalled
        params['load_res_if_file_exists'] = False
        params['save_res'] = True

    elif analysis == 'classify_rec':
        params['ana_class'] = subject_classifier.SubjectClassifier
        params['train_phase'] = ['rec']
        params['test_phase'] = ['rec']
        params['norm'] = 'l2'
        params['recall_filter_func'] = ram_data_helpers.filter_events_to_recalled
        params['load_res_if_file_exists'] = False
        params['save_res'] = True

    elif analysis == 'classify_both':
        params['ana_class'] = subject_classifier.SubjectClassifier
        params['train_phase'] = ['enc', 'rec']
        params['test_phase'] = ['enc', 'rec']
        params['scale_enc'] = 2.0
        params['norm'] = 'l2'
        params['recall_filter_func'] = ram_data_helpers.filter_events_to_recalled
        params['load_res_if_file_exists'] = False
        params['save_res'] = True

    else:
        print('Invalid analysis: %s' % analysis)
        return {}

    if subject_settings == 'default':
        task = 'RAM_TH1'
        params['task'] = task
        params['subjs'] = ram_data_helpers.get_subjs(task)
        params['feat_phase'] = ['enc', 'rec_circle']
        params['feat_type'] = 'power'
        params['start_time'] = [-1.2, -2.9]
        params['end_time'] = [0.5, -0.2]
        params['bipolar'] = True
        params['freqs'] = np.logspace(np.log10(1), np.log10(200), 8)
    else:
        print('Invalid subject settings: %s' % subject_settings)
        return {}

    return params
