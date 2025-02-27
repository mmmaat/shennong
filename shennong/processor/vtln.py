"""Extraction of VTLN warp factors from utterances.

Uses the Kaldi implmentation of Linear Vocal Tract Length Normalization
(see [kaldi-lvtln]_).

Examples
--------

>>> from shennong import Utterances
>>> from shennong.processor.vtln import VtlnProcessor
>>> wav = './test/data/test.wav'
>>> utterances = Utterances(
...     [('utt1', wav, 'spk1', 0, 1), ('utt2', wav, 'spk1', 1, 1.4)])

Initialize the VTLN model. Other options can be specified at construction,
or after:

>>> vtln = VtlnProcessor(min_warp=0.95, max_warp=1.05, ubm={'num_gauss': 4})
>>> vtln.num_iters = 10

Returns the computed warps for each utterance. If the ``by_speaker`` property
was set to ``True`` and the speaker information is provided with the
utterances, the warps have been computed for each speaker, and each utterance
from the same speaker is mapped to the same warp factor.

>>> warps = vtln.process(utterances)

Those warps can be passed individually in the :func:`process` method of
:class:`~shennong.features.processor.mfcc.MfccProcessor`,
:class:`~shennong.features.processor.filterbank.FilterbankProcessor`,
:class:`~shennong.features.processor.plp.PlpProcessor` and
:class:`~shennong.features.processor.spectrogram.SpectrogramProcessor`
to warp the corresponding feature.

The features can also be warped directly via the pipeline.

>>> from shennong.pipeline import get_default_config, extract_features
>>> config = get_default_config('mfcc', with_vtln='simple')
>>> config['vtln']['ubm']['num_gauss'] = 4
>>> warped_features = extract_features(config, utterances)

References
----------
.. [kaldi-lvtln] https://kaldi-asr.org/doc/transform.html#transform_lvtln

"""

import copy
import os
import yaml

import numpy as np
import kaldi.matrix
import kaldi.matrix.common
import kaldi.matrix.functions
import kaldi.transform
import kaldi.util.io

from shennong import pipeline, FeaturesCollection, Features
from shennong.base import BaseProcessor
from shennong.logger import null_logger
from shennong.processor.ubm import DiagUbmProcessor
from shennong.postprocessor.cmvn import SlidingWindowCmvnPostProcessor
from shennong.postprocessor.vad import VadPostProcessor


class VtlnProcessor(BaseProcessor):
    """VTLN model"""
    def __init__(self, num_iters=15, min_warp=0.85,
                 max_warp=1.25, warp_step=0.01,
                 logdet_scale=0.0, norm_type='offset',
                 subsample=5, features=None,
                 ubm=None, by_speaker=True):
        super().__init__()

        self.num_iters = num_iters
        self.min_warp = min_warp
        self.max_warp = max_warp
        self.warp_step = warp_step
        self.logdet_scale = logdet_scale
        self.norm_type = norm_type
        self.subsample = subsample
        self.by_speaker = by_speaker

        if features in (None, 'default'):
            config = pipeline.get_default_config('mfcc', with_delta=True)
            config['sliding_window_cmvn'] = (
                SlidingWindowCmvnPostProcessor().get_params())
            config['sliding_window_cmvn']['cmn_window'] = 300
            config['delta']['window'] = 3
            self.features = config
        else:
            self.features = features

        if ubm is None:
            default_num_gauss = 64
            self.ubm = DiagUbmProcessor(default_num_gauss).get_params()
        else:
            self.ubm = ubm

        self.lvtln = None
        self.transforms = None
        self.warps = None

    @property
    def name(self):
        return 'vtln'

    @property
    def num_iters(self):
        """Number of iterations of training"""
        return self._num_iters

    @num_iters.setter
    def num_iters(self, value):
        self._num_iters = int(value)

    @property
    def min_warp(self):
        """Minimum warp considered"""
        return self._min_warp

    @min_warp.setter
    def min_warp(self, value):
        self._min_warp = float(value)

    @property
    def max_warp(self):
        """Maximum warp considered"""
        return self._max_warp

    @max_warp.setter
    def max_warp(self, value):
        self._max_warp = float(value)

    @property
    def warp_step(self):
        """Warp step"""
        return self._warp_step

    @warp_step.setter
    def warp_step(self, value):
        self._warp_step = float(value)

    @property
    def logdet_scale(self):
        """Scale on log-determinant term in auxiliary function"""
        return self._logdet_scale

    @logdet_scale.setter
    def logdet_scale(self, value):
        self._logdet_scale = float(value)

    @property
    def norm_type(self):
        """Type of fMLLR applied (``offset``, ``none`` or ``diag``)"""
        return self._norm_type

    @norm_type.setter
    def norm_type(self, value):
        if value not in ['offset', 'none', 'diag']:
            raise ValueError('Invalid norm type {}'.format(value))
        self._norm_type = value

    @property
    def subsample(self):
        """When computing base LVTLN transforms, use every n frames
         (a speedup)"""
        return self._subsample

    @subsample.setter
    def subsample(self, value):
        self._subsample = int(value)

    @property
    def by_speaker(self):
        """Compute the warps for each speaker, or each utterance"""
        return self._by_speaker

    @by_speaker.setter
    def by_speaker(self, value):
        self._by_speaker = bool(value)

    @property
    def features(self):
        """Features extraction configuration"""
        return self._features

    @features.setter
    def features(self, value):
        if not isinstance(value, dict):
            raise TypeError('Features extraction configuration must be a dict')
        if 'mfcc' not in value:
            raise ValueError('Need mfcc features to train VTLN model')
        self._features = copy.deepcopy(value)

    @property
    def ubm(self):
        "Diagonal UBM-GMM configuration"
        return self._ubm

    @ubm.setter
    def ubm(self, value):
        if not isinstance(value, dict):
            raise TypeError('UBM configuration must be a dict')
        ubm_keys = DiagUbmProcessor(2).get_params().keys()
        if not value.keys() <= ubm_keys:
            raise ValueError('Unknown parameters given for UBM config')
        self._ubm = copy.deepcopy(value)

    @classmethod
    def load(cls, path):
        """Load the LVTLN from a binary file"""
        if not os.path.isfile(path):
            raise OSError('{}: file not found'.format(path))

        vtln = VtlnProcessor()
        vtln.lvtln = kaldi.transform.lvtln.LinearVtln.new(0, 1, 0)
        with kaldi.util.io.xopen(path, mode='rb') as handler:
            vtln.lvtln.read(handler.stream(), binary=True)
        return vtln

    @classmethod
    def load_warps(cls, path):
        """Load precomputed warps"""
        if not os.path.isfile(path):
            raise OSError('{}: file not found'.format(path))
        try:
            warps = yaml.load(open(path, 'r'), Loader=yaml.FullLoader)
        except yaml.YAMLError as err:  # pragma: nocover
            raise ValueError(
                'Error in VTLN warps file when loading: {}'.format(err))
        return warps

    def save(self, path):
        """Save the LVTLN to a binary file"""
        if os.path.isfile(path):
            raise OSError('{}: file already exists'.format(path))

        if not isinstance(self.lvtln, kaldi.transform.lvtln.LinearVtln):
            raise TypeError('VTLN not initialized')

        with kaldi.util.io.xopen(path, mode='wb') as handler:
            self.lvtln.write(handler.stream(), binary=True)

    def save_warps(self, path):
        """Save the computed warps"""
        if os.path.isfile(path):
            raise OSError('{}: file already exists'.format(path))

        if not isinstance(self.warps, dict):
            raise TypeError('Warps not computed')
        try:
            yaml.dump(self.warps, open(path, 'w'))
        except yaml.YAMLError as err:  # pragma: nocover
            raise ValueError(
                'Error in VTLN warps file when saving: {}'.format(err))

    def compute_mapping_transform(
            self, feats_untransformed, feats_transformed,
            class_idx, warp, weights=None):
        """"Set one of the transforms in lvtln to the minimum-squared-error solution
        to mapping feats_untransformed to feats_transformed; posteriors may
        optionally be used to downweight/remove silence.

        Adapted from [kaldi-train-lvtln-special]_

        Parameters
        ----------
        feats_untransformed : FeaturesCollection
            Collection of original features.
        feats_transformed :
            Collection of warped features.
        class_idx : int
            Rank of warp considered.
        warp : float, optional
            Warp considered.
        weights : dict[str, ndarrays], optional
            For each features in the collection, an array of weights to
            apply on the features frames. Unweighted by default.

        Raises
        ------
        ValueError
            If the features have unconsistent dimensions. If the size of the
            posteriors does not correspond to the size of the features.

        References
        ----------
        .. [kaldi-train-lvtln-special]
            https://kaldi-asr.org/doc/gmm-train-lvtln-special_8cc.html

        """
        # Normalize diagonal of variance to be the same before and after
        # transform. We are not normalizing the full covariance
        if not isinstance(self.lvtln, kaldi.transform.lvtln.LinearVtln):
            raise TypeError('VTLN not initialized')
        dim = self.lvtln.dim()
        Q = kaldi.matrix.packed.SpMatrix(dim+1)
        l = kaldi.matrix.Matrix(dim, dim+1)
        c = kaldi.matrix.Vector(dim)
        beta = 0.0
        sum_xplus, sumsq_x, sumsq_diff = kaldi.matrix.Vector(
            dim+1), kaldi.matrix.Vector(dim), kaldi.matrix.Vector(dim)
        for utt in feats_untransformed:
            if utt not in feats_transformed:
                raise ValueError(f'No transformed features for key {utt}')
            x_feats = kaldi.matrix.SubMatrix(feats_untransformed[utt].data)
            y_feats = kaldi.matrix.SubMatrix(feats_transformed[utt].data)
            if x_feats.num_rows != y_feats.num_rows or \
                    x_feats.num_cols != y_feats.num_cols or \
                    x_feats.num_cols != dim:
                raise ValueError('Number of rows and/or columns differs: '
                                 f'{x_feats.num_rows} vs {y_feats.num_rows} '
                                 f'rows, {x_feats.num_cols} vs '
                                 f'{y_feats.num_cols} columns, {dim} dim')

            this_weights = kaldi.matrix.Vector(x_feats.num_rows)
            if weights is not None:
                if utt not in weights:
                    raise ValueError(f'No weights for utterance {utt}')
                this_weights.copy_(kaldi.matrix.SubVector(weights[utt]))
            else:
                this_weights.add_(1)
            for i in range(x_feats.num_rows):
                weight = this_weights[i]
                x_row = kaldi.matrix.SubVector(x_feats.row(i))
                y_row = kaldi.matrix.SubVector(y_feats.row(i))
                xplus_row_dbl = kaldi.matrix.Vector(x_row)
                xplus_row_dbl.resize_(
                    dim+1, kaldi.matrix.common.MatrixResizeType.COPY_DATA)
                xplus_row_dbl[dim] = 1.0
                y_row_dbl = kaldi.matrix.Vector(y_row)
                Q.add_vec2_(weight, xplus_row_dbl)
                l.add_vec_vec_(weight, y_row_dbl, xplus_row_dbl)
                beta += weight

                sum_xplus.add_vec_(weight, xplus_row_dbl)
                sumsq_x.add_vec2_(weight, x_row)
                sumsq_diff.add_vec2_(weight, x_row)
                sumsq_diff.add_vec2_(weight, y_row)
                sumsq_diff.add_vec_vec_(-2*weight, x_row, y_row, 1)
                c.add_vec2_(weight, y_row)
        A = kaldi.matrix.Matrix(dim, dim)
        Qinv = kaldi.matrix.packed.SpMatrix(Q.num_rows)
        Qinv.copy_from_sp_(Q)
        Qinv.invert_()
        for i in range(dim):
            w_i = kaldi.matrix.Vector(dim+1)
            l_i = kaldi.matrix.SubVector(l.row(i))
            w_i.add_mat_vec_(
                1.0, Qinv,
                kaldi.matrix.common.MatrixTransposeType.NO_TRANS,
                l_i, 0.0)
            a_i = kaldi.matrix.SubVector(w_i, 0, dim)
            A.row(i).copy_(a_i)
            error = (kaldi.matrix.functions.vec_vec(
                w_i, kaldi.matrix.Vector(dim+1).add_mat_vec_(
                    1.0, Q,
                    kaldi.matrix.common.MatrixTransposeType.NO_TRANS,
                    w_i, 0.0))
                - 2*kaldi.matrix.functions.vec_vec(w_i, l_i) + c[i])/beta
            sqdiff = sumsq_diff[i]/beta
            scatter = sumsq_x[i]/beta
            self.log.debug(
                'For dimension %s sum-squared error in linear approximation '
                'is %s, versus feature-difference %s, orig-sumsq is %s',
                i, error, sqdiff, scatter)

            # Normalize variance
            x_var = scatter - (sum_xplus[i]/beta)**2
            y_var = kaldi.matrix.functions.vec_vec(
                w_i, kaldi.matrix.Vector(dim+1).add_mat_vec_(
                    1.0, Q,
                    kaldi.matrix.common.MatrixTransposeType.NO_TRANS,
                    w_i, 0.0))/beta \
                - (kaldi.matrix.functions.vec_vec(w_i, sum_xplus)/beta)**2
            scale = np.sqrt(x_var/y_var)
            A.row(i).scale_(scale)
        self.lvtln.set_transform(class_idx, A)
        self.lvtln.set_warp(class_idx, warp)

    def estimate(self, ubm,
                 feats_collection,
                 posteriors, utt2speak=None):
        """Estimate linear-VTLN transforms, either per utterance or for
        the supplied set of speakers (``utt2speak`` option).
        Reads posteriors indicating Gaussian indexes in the UBM.

        Adapted from [kaldi-global-est-lvtln-trans]_

        Parameters
        ----------
        ubm : DiagUbmProcessor
            The Universal Background Model.
        feats_collection : FeaturesCollection
            The untransformed features.
        posteriors : dict[str, list[list[tuple[int, float]]]]
            The posteriors indicating Gaussian indexes in the UBM.
        utt2speak : dict[str, str], optional
            If provided, map each utterance to a speaker.

        References
        ----------
        .. [kaldi-global-est-lvtln-trans]
            https://kaldi-asr.org/doc/gmm-global-est-lvtln-trans_8cc.html

        """
        if not isinstance(self.lvtln, kaldi.transform.lvtln.LinearVtln):
            raise TypeError('VTLN not initialized')
        transforms = {}
        warps = {}
        tot_lvtln_impr, tot_t = 0.0, 0.0
        class_counts = kaldi.matrix.Vector(self.lvtln.num_classes())
        class_counts.set_zero_()

        if utt2speak is not None:  # per speaker adaptation
            spk2utt2feats = feats_collection.partition(utt2speak)
            for spk in spk2utt2feats:
                spk_stats = kaldi.transform.mllr.FmllrDiagGmmAccs.from_dim(
                    self.lvtln.dim())
                # Accumulate stats over all utterances of the current speaker
                for utt in spk2utt2feats[spk]:
                    if utt not in posteriors:
                        raise ValueError(f'No posterior for utterance {utt}')
                    feats = kaldi.matrix.SubMatrix(
                        spk2utt2feats[spk][utt].data)
                    post = posteriors[utt]
                    if len(post) != feats.num_rows:
                        raise ValueError(
                            f'Posterior has wrong size {len(post)}'
                            f' vs {feats.num_rows}')
                    # Accumulate for utterance
                    for i in range(len(post)):
                        gselect = []
                        this_post = kaldi.matrix.Vector(len(post[i]))
                        for j in range(len(post[i])):
                            gselect.append(post[i][j][0])
                            this_post[j] = post[i][j][1]
                        spk_stats.accumulate_from_posteriors_preselect(
                            ubm.gmm, gselect, feats.row(i), this_post)

                # Compute the transform
                transform = kaldi.matrix.Matrix(
                    self.lvtln.dim(), self.lvtln.dim()+1)
                class_idx, _, objf_impr, count = (
                    self.lvtln.compute_transform(spk_stats,
                                                 self.norm_type,
                                                 self.logdet_scale,
                                                 transform))
                class_counts[class_idx] += 1
                transforms[spk] = transform
                warps[spk] = self.lvtln.get_warp(class_idx)

                self.log.debug(
                    'speaker %s: auxf-impr from LVTLN is %s, over %s frames',
                    spk, objf_impr / count, count)

                tot_lvtln_impr += objf_impr
                tot_t += count

        else:  # per utterance adaptation
            for utt in feats_collection:
                if utt not in posteriors:
                    raise ValueError(f'No posterior for utterance {utt}')
                feats = kaldi.matrix.Matrix(feats_collection[utt].data)
                post = posteriors[utt]
                if len(post) != feats.num_rows:
                    raise ValueError(f'Posterior has wrong size {len(post)}'
                                     f' vs {feats.num_rows}')
                spk_stats = kaldi.transform.mllr.FmllrDiagGmmAccs.from_dim(
                    self.lvtln.dim())

                # Accumulate for utterance
                for i in range(len(post)):
                    gselect = []
                    this_post = kaldi.matrix.Vector(len(post[i]))
                    for j in range(len(post[i])):
                        gselect.append(post[i][j][0])
                        this_post[j] = post[i][j][1]
                    spk_stats.accumulate_from_posteriors_preselect(
                        ubm.gmm, gselect, feats.row(i), this_post)

                # Compute the transform
                transform = kaldi.matrix.Matrix(
                    self.lvtln.dim(), self.lvtln.dim()+1)
                class_idx, _, objf_impr, count = \
                    self.lvtln.compute_transform(
                        spk_stats,
                        self.norm_type,
                        self.logdet_scale,
                        transform)

                class_counts[class_idx] += 1
                transforms[utt] = transform
                warps[utt] = self.lvtln.get_warp(class_idx)
                self.log.debug(
                    'utterance %s: auxf-impr from LVTLN is %s, over %s frames',
                    utt, objf_impr / count, count)
                tot_lvtln_impr += objf_impr
                tot_t += count

        message = 'Distribution of classes is'
        for count in class_counts:
            message += " "+str(count)
        message += f', overall LVTLN auxfimpr per' \
            f' frame is {tot_lvtln_impr/tot_t}  over {tot_t} frames'
        self.log.debug(message)
        return transforms, warps

    def process(self, utterances, ubm=None, group_by='utterance', njobs=1):
        """Compute the VTLN warp factors for the given utterances.

        If the ``by_speaker`` option is set to True before the call to
        :func:`process()`, the warps are computed on per speaker basis (i.e.
        each utterance of the same speaker has an identical warp). If
        ``per_speaker`` is False, the warps are computed on a per-utterance
        basis.

        Parameters
        ----------
        utterances : :class:`~shennong.utterances.Utterances`
            The list of utterances to train the VTLN on.
        ubm : DiagUbmProcessor, optional
            If provided, uses this UBM instead of computing a new one.
        group_by : str, optional
            Must be 'utterance' or 'speaker'.
        njobs : int, optional
            Number of threads to use for computation, default to 1.

        Returns
        -------
        warps : dict[str, float]
            Warps computed for each speaker or utterance, according to
            ``group_by``. If by speaker: same warp for all utterances of this
            speaker.

        """
        if group_by not in ('utterance', 'speaker'):
            raise ValueError(
                f'group_by must be "utterance" or "speaker", '
                f'it is: {group_by}')
        if group_by == 'speaker' and not self.by_speaker:
            raise ValueError(
                'Asking to group warps by speaker but they are computed '
                'per utterance, please set VtlnProcessor.by_speaker to True')
        if self.by_speaker and not utterances.has_speakers():
            raise ValueError(
                'Requested speaker based VTLN, but speaker'
                ' information is missing')

        utt2speak = None
        if self.by_speaker:
            utt2speak = {utt.name: utt.speaker for utt in utterances}

        # Min / max warp
        if self.min_warp > self.max_warp:
            raise ValueError(
                f'Min warp > max warp: {self.min_warp} > {self.max_warp}')

        # UBM-GMM
        if ubm is None:
            ubm = DiagUbmProcessor(**self.ubm)
            ubm.log.setLevel(self.log.getEffectiveLevel())
            ubm.process(utterances, njobs=njobs)
        else:
            if ubm.gmm is None:
                raise ValueError('Given UBM-GMM has not been trained')
            self.ubm = ubm.get_params()

        self.log.info('Initializing base LVTLN transforms')
        dim = ubm.gmm.dim()
        num_classes = int(1.5 + (self.max_warp-self.min_warp) / self.warp_step)
        default_class = int(0.5 + (1-self.min_warp)/self.warp_step)
        self.lvtln = kaldi.transform.lvtln.LinearVtln.new(
            dim, num_classes, default_class)

        cmvn_config = self.features.pop('sliding_window_cmvn', None)

        raw_mfcc = pipeline.extract_features(
            self.features, utterances, njobs=njobs, log=null_logger())

        # Compute VAD decision
        self.log.debug('... computing VAD decision')
        vad = {}
        for utt, mfcc in raw_mfcc.items():
            this_vad = VadPostProcessor(**ubm.vad).process(mfcc)
            vad[utt] = this_vad.data.reshape(
                (this_vad.shape[0],)).astype(bool)

        # Apply cmvn sliding
        orig_features = FeaturesCollection()
        if cmvn_config is not None:
            proc = SlidingWindowCmvnPostProcessor(**cmvn_config)
            for utt, mfcc in raw_mfcc.items():
                orig_features[utt] = proc.process(mfcc)
        else:
            orig_features = raw_mfcc

        # Select voiced frames
        orig_features = orig_features.trim(vad)
        orig_features = FeaturesCollection(  # Subsample
            {utt: feats.copy(subsample=self.subsample)
             for utt, feats in orig_features.items()})

        # Computing base transforms
        featsub_unwarped = pipeline.extract_features(
            self.features, utterances,
            njobs=njobs, log=null_logger()).trim(vad)
        featsub_unwarped = FeaturesCollection(
            {utt: feats.copy(subsample=self.subsample)
             for utt, feats in featsub_unwarped.items()})

        for c in range(num_classes):
            this_warp = self.min_warp + c*self.warp_step
            self.log.info(
                'Computing base transform (warp=%s) %s/%s',
                this_warp, c+1, num_classes)

            featsub_warped = pipeline.extract_features_warp(
                self.features, utterances, this_warp,
                null_logger(), njobs=njobs).trim(vad)
            featsub_warped = FeaturesCollection(
                {utt: feats.copy(subsample=self.subsample)
                 for utt, feats in featsub_warped.items()})
            self.compute_mapping_transform(
                featsub_unwarped, featsub_warped, c, this_warp)

        del featsub_warped, featsub_unwarped, vad

        if cmvn_config is not None:
            self.features['sliding_window_cmvn'] = cmvn_config

        self.log.debug('Computing Gaussian selection info')
        ubm.gaussian_selection(orig_features)

        self.log.info(
            'Computing LVTLN transforms (%s iterations)', self.num_iters)
        posteriors = ubm.gaussian_selection_to_post(orig_features)
        self.transforms, self.warps = self.estimate(
            ubm, orig_features, posteriors, utt2speak)

        for i in range(self.num_iters):
            self.log.debug('Updating model on pass %s/%s', i+1, self.num_iters)

            # Transform the features
            features = FeaturesCollection()
            for utt, feats in orig_features.items():
                ind = utt if utt2speak is None else utt2speak[utt]
                linear_part = self.transforms[ind][:, : feats.ndims]
                offset = self.transforms[ind][:, feats.ndims]
                data = np.dot(feats.data, linear_part.numpy().T) + \
                    offset.numpy()
                features[utt] = Features(data, feats.times, feats.properties)

            # Update the model
            gmm_accs = ubm.accumulate(features, njobs=njobs)
            ubm.estimate(gmm_accs)

            # Now update the LVTLN transforms (and warps)
            # self.log.debug('Re-estimating LVTLN transforms on pass %s', i+1)
            posteriors = ubm.gaussian_selection_to_post(features)
            self.transforms, self.warps = self.estimate(
                ubm, orig_features, posteriors, utt2speak)

        if self.by_speaker:
            self.transforms = {
                utt: self.transforms[spk]
                for utt, spk in utt2speak.items()}
            self.warps = {
                utt: self.warps[spk]
                for utt, spk in utt2speak.items()}

        self.log.info('Done training LVTLN model')
        if group_by == 'utterance':
            return self.warps
        # group_by == 'speaker'
        return {
            spk: self.warps[utts[0].name]
            for spk, utts in utterances.by_speaker().items()}
