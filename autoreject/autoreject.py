"""Auto reject."""

# Authors: Mainak Jas <mainak.jas@telecom-paristech.fr>
#          Alexandre Gramfort <alexandre.gramfort@telecom-paristech.fr>

import numpy as np

import mne

from sklearn.base import BaseEstimator
from sklearn.grid_search import RandomizedSearchCV
from sklearn.cross_validation import KFold

from progressbar import ProgressBar, SimpleProgress
from joblib import Memory
from pandas import DataFrame
from scipy.stats.distributions import uniform
from collections import namedtuple
from mne.utils import logger

from .utils import clean_by_interp

mem = Memory(cachedir='cachedir')


def grid_search(epochs, n_interpolates, consensus_percs, prefix, n_folds=3):
    """
    Parameters
    ----------
    epochs : instance of mne.Epochs
        The epochs object for which bad epochs must be found.
    n_interpolates : array
        The number of sensors to interpolate.
    consensus_percs : array
        The percentage of channels to be interpolated.
    n_folds : int
        Number of folds for cross-validation.
    prefix : str
        Prefix to the log
    """
    cv = KFold(len(epochs), n_folds=n_folds, random_state=42)
    err_cons = np.zeros((len(consensus_percs), len(n_interpolates),
                         n_folds))

    auto_reject = ConsensusAutoReject()
    # The thresholds must be learnt from the entire data
    auto_reject.fit(epochs)

    for fold, (train, test) in enumerate(cv):
        for jdx, n_interp in enumerate(n_interpolates):
            for idx, consensus_perc in enumerate(consensus_percs):
                logger.info('%s[Val fold %d] Trying consensus '
                            'perc %0.2f, n_interp %d' % (
                                prefix, fold + 1, consensus_perc, n_interp))
                # set the params
                auto_reject.consensus_perc = consensus_perc
                auto_reject.n_interpolate = n_interp
                # not do the transform
                auto_reject.transform(epochs[train])
                # score using this param
                X = epochs[test].get_data()
                err_cons[idx, jdx, fold] = -auto_reject.score(X)

    return err_cons


class BaseAutoReject(BaseEstimator):
    """Base class for rejection."""

    def score(self, X):
        if hasattr(self, 'n_channels'):
            X = X.reshape(-1, self.n_channels, self.n_times)
        if np.any(np.isnan(self.mean_)):
            return -np.inf
        else:
            return -np.sqrt(np.mean((np.median(X, axis=0) - self.mean_) ** 2))

    def fit_transform(self, epochs):
        """Estimates the rejection params and finds bad epochs.

        Parameters
        ----------
        epochs : instance of mne.Epochs
            The epochs object which must be cleaned.
        """
        return self.fit(epochs).transform(epochs)


class GlobalAutoReject(BaseAutoReject):
    """docstring for AutoReject."""

    def __init__(self, n_channels, n_times, thresh=40e-6):
        self.thresh = thresh
        self.n_channels = n_channels
        self.n_times = n_times

    def fit(self, X, y=None):
        X = X.reshape(-1, self.n_channels, self.n_times)
        deltas = np.array([np.ptp(d, axis=1) for d in X])
        epoch_deltas = deltas.max(axis=1)
        keep = epoch_deltas <= self.thresh
        self.mean_ = np.mean(X[keep], axis=0)
        return self


class ChannelAutoReject(BaseAutoReject):
    """docstring for AutoReject"""

    def __init__(self, thresh=40e-6):
        self.thresh = thresh

    def fit(self, X, y=None):
        """
        Parameters
        ----------
        X : array, shape (n_epochs, n_times)
            The data for one channel.
        y : None
            Redundant. Necessary to be compatible with sklearn
            API.
        """
        deltas = np.ptp(X, axis=1)
        self.deltas_ = deltas
        keep = deltas <= self.thresh
        self.mean_ = np.mean(X[keep], axis=0)
        return self


def _pick_exclusive_channels(info, ch_type):
    if ch_type == 'eeg':
        picks = mne.pick_types(info, meg=False, eeg=True)
    elif ch_type == 'eog':
        picks = mne.pick_types(info, meg=False, eog=True)
    elif ch_type == 'meg':
        picks = mne.pick_types(info, meg=True)
    elif ch_type == 'grad' or ch_type == 'mag':
        picks = mne.pick_types(info, meg=True)
    return picks


def _rank_channels(data):
    """Rank the channels epoch-wise
    """
    deltas = np.ptp(data, axis=-1).T
    n_channels = deltas.shape[0]
    n_epochs = deltas.shape[1] / 2
    scores = np.zeros((n_epochs, n_channels))
    for ch_idx, delta in enumerate(deltas):
        for epoch_idx in range(n_epochs):
            scores[epoch_idx, ch_idx] = np.mean(delta[epoch_idx] <= delta)
    return scores


def _compute_thresh(this_data, ch_type, cv=10):
    """ Compute the rejection threshold for one channel.

    Parameters
    ----------
    this_data: array (n_epochs, n_times)
        Data for one channel.
    ch_type: str
        'mag', 'grad' or 'eeg'.
    cv : iterator
        Iterator for cross-validation.
    """
    est = ChannelAutoReject()

    Limits = namedtuple('Limits', 'low high')
    limits = dict(eeg=Limits(low=20e-7, high=400e-6),
                  grad=Limits(low=400e-13, high=20000e-13),
                  mag=Limits(low=400e-15, high=20000e-15))

    param_dist = dict(thresh=uniform(limits[ch_type].low,
                                     limits[ch_type].high))
    rs = RandomizedSearchCV(est,  # XXX : is random really better than grid?
                            param_distributions=param_dist,
                            n_iter=20, cv=cv)
    rs.fit(this_data)
    best_thresh = rs.best_estimator_.thresh

    return best_thresh


def compute_threshes(epochs):
    """ Compute thresholds for each channel.
    """
    ch_types = [ch_type for ch_type in ('eeg', 'meg')
                if ch_type in epochs]
    epochs_interp = clean_by_interp(epochs)
    data = np.concatenate((epochs.get_data(), epochs_interp.get_data()),
                          axis=0)
    threshes = dict()
    scores = dict()
    picks_grad, picks_mag = list(), list()
    for ch_type in ch_types:
        print('Compute optimal thresholds for %s' % ch_type)
        picks = _pick_exclusive_channels(epochs.info, ch_type)
        if ch_type == 'meg':
            picks_grad = _pick_exclusive_channels(epochs.info, 'grad')
            picks_mag = _pick_exclusive_channels(epochs.info, 'mag')
        np.random.seed(42)  # has no effect unless shuffle=True is used
        cv = KFold(data.shape[0], 10, random_state=42)
        threshes[ch_type] = []
        scores[ch_type] = _rank_channels(data[:, picks])
        for ii, pick in enumerate(picks):
            if pick in picks_grad:
                thresh_type = 'grad'
            elif pick in picks_mag:
                thresh_type = 'mag'
            else:
                thresh_type = 'eeg'
            thresh = _compute_thresh(data[:, pick], ch_type=thresh_type, cv=cv)
            threshes[ch_type].append(thresh)
    return threshes, scores


class ConsensusAutoReject(BaseAutoReject):
    """ Class to deal with automatically rejecting bad epochs.

    Parameters
    ----------
    epochs : instance of mne.Epochs
        The epochs object
    thresh_func : callable | None
        Function which returns the channel-level thresholds. If None,
        defaults to ``autoreject.compute_threshes``.
    consensus_perc : float (0 to 1.0)
        percentage of channels that must agree as a fraction of
        the total number of channels.
    n_interpolate : int (default 0)
        Number of channels for which to interpolate
    """
    def __init__(self, thresh_func=None, consensus_perc=0.1, n_interpolate=0):

        # TODO: must be able to try different consensus percs
        # with pretrained thresh
        if thresh_func is None:
            thresh_func = compute_threshes
        if not (0 <= consensus_perc <= 1):
            raise ValueError('"consensus_perc" must be between 0 and 1. '
                             'You gave me %s.' % consensus_perc)
        self.consensus_perc = consensus_perc
        self.n_interpolate = n_interpolate
        self.thresh_func = mem.cache(thresh_func)

    def _check_data(self, epochs):
        getattr(epochs, 'drop_bad', 'drop_bad_epochs')()
        if any(len(drop) > 0 and drop != ['IGNORED']
                for drop in epochs.drop_log):
            raise RuntimeError('Some epochs are being dropped (maybe due to '
                               'incomplete data). Please check that no epoch '
                               'is dropped.')

    def fit(self, epochs):
        """Compute the thresholds.

        Parameters
        ----------
        epochs : instance of mne.Epochs
            The epochs object from which the channel-level thresholds are
            estimated.
        """
        self.threshes_, self.scores_ = self.thresh_func(epochs)
        return self

    def transform(self, epochs):
        """Fixes and finds the bad epochs.

        Parameters
        ----------
        epochs : instance of mne.Epochs
            The epochs object for which bad epochs must be found.
        """
        epochs = epochs.copy()
        self._check_data(epochs)

        self._vote_epochs(epochs)
        ch_types = [ch_type for ch_type in ('eeg', 'meg') if ch_type in epochs]
        for ch_type in ch_types:
            self._interpolate_bad_epochs(epochs, ch_type=ch_type)

        bad_epochs_idx = self._get_bad_epochs()
        self.bad_epochs_idx = np.sort(bad_epochs_idx)
        self.good_epochs_idx = np.setdiff1d(np.arange(len(epochs)),
                                            bad_epochs_idx)
        self.mean_ = epochs[self.good_epochs_idx].get_data().mean(axis=0)
        return epochs[self.good_epochs_idx]

    def _vote_epochs(self, epochs):
        """Each channel votes for an epoch as good or bad

        Parameters
        ----------
        epochs : instance of mne.Epochs
            The epochs object for which bad epochs must be found.
        """
        n_epochs = len(epochs)
        picks = mne.pick_types(epochs.info, meg=True, eeg=True, eog=True)
        self.drop_log = DataFrame(np.zeros((n_epochs, len(picks)), dtype=int),
                                  columns=epochs.info['ch_names'])
        self.bad_epoch_counts = np.zeros((len(epochs), ))
        ch_types = [ch_type for ch_type in ('eeg', 'meg')
                    if ch_type in epochs]
        for ch_type in ch_types:
            picks = _pick_exclusive_channels(epochs.info, ch_type)
            ch_names = [epochs.info['ch_names'][p] for p in picks]
            deltas = np.ptp(epochs.get_data()[:, picks], axis=-1).T
            threshes = self.threshes_[ch_type]
            for delta, thresh, ch_name in zip(deltas, threshes, ch_names):
                bad_epochs_idx = np.where(delta > thresh)[0]
                # TODO: combine for different ch types
                self.bad_epoch_counts[bad_epochs_idx] += 1
                self.drop_log.ix[bad_epochs_idx, ch_name] = 1

    def _get_bad_epochs(self):
        """Get the indices of bad epochs.
        """
        # TODO: this must be done separately for each channel type?
        self.sorted_epoch_idx = np.argsort(self.bad_epoch_counts)[::-1]
        bad_epoch_counts = np.sort(self.bad_epoch_counts)[::-1]
        n_channels = self.drop_log.shape[1]
        n_consensus = self.consensus_perc * n_channels
        if np.max(bad_epoch_counts) >= n_consensus:
            self.n_epochs_drop = np.sum(self.bad_epoch_counts
                                        >= n_consensus) + 1
            bad_epochs_idx = self.sorted_epoch_idx[:self.n_epochs_drop]
        else:
            self.n_epochs_drop = 0
            bad_epochs_idx = []
            print('No bad epochs dropped by consensus.')

        return bad_epochs_idx

    def _interpolate_bad_epochs(self, epochs, ch_type):
        """interpolate the bad epochs.

        Parameters
        ----------
        epochs : instance of mne.Epochs
            The epochs object which must be fixed.
        """
        from utils import interpolate_bads
        drop_log = self.drop_log
        # 1: bad segment, # 2: interpolated, # 3: dropped
        self.fix_log = self.drop_log.copy()
        ch_names = drop_log.columns.values
        n_consensus = self.consensus_perc * len(ch_names)
        pbar = ProgressBar(widgets=[SimpleProgress()])
        print('Repairing epochs: ')
        # TODO: raise error if preload is not True
        for epoch_idx in pbar(range(len(epochs))):
            # ch_score = self.scores_[ch_type][epoch_idx]
            # sorted_ch_idx = np.argsort(ch_score)
            n_bads = drop_log.ix[epoch_idx].sum()
            if n_bads == 0 or n_bads > n_consensus:
                continue
            else:
                if n_bads <= self.n_interpolate:
                    bad_chs = drop_log.ix[epoch_idx].values == 1
                else:
                    # get peak-to-peak for channels in that epoch
                    data = epochs[epoch_idx].get_data()[0, :, :]
                    peaks = np.ptp(data, axis=-1)
                    # find channels which are bad by rejection threshold
                    bad_chs = np.where(drop_log.ix[epoch_idx].values == 1)[0]
                    # find the ordering of channels amongst the bad channels
                    sorted_ch_idx = np.argsort(peaks[bad_chs])[::-1]
                    # then select only the worst n_interpolate channels
                    bad_chs = bad_chs[sorted_ch_idx[:self.n_interpolate]]

            self.fix_log.ix[epoch_idx][bad_chs] = 2
            bad_chs = ch_names[bad_chs].tolist()
            epoch = epochs[epoch_idx]
            epoch.info['bads'] = bad_chs
            interpolate_bads(epoch, reset_bads=True)
            epochs._data[epoch_idx] = epoch._data
