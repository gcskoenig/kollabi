import numpy as np
import pandas as pd
import scipy.special 
import math
import tqdm
import itertools
import logging
import time

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import seaborn as sns

from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_squared_error

from kollabi.plots import forceplot
from kollabi.explanation import Explanation, SurplusExplanation, CollabExplanation

interpret_logger = logging.getLogger('interpret')
interpret_logger.setLevel(logging.WARNING)

idx = pd.IndexSlice


class CollabExplainer:
    """
    A class for computing feature decompositions and collaboration measures in a dataset.

    Parameters:
        df (pandas.DataFrame): The input dataset.
        target (str): The target variable name.
        test_size (float, optional): The proportion of the dataset to include in the test split. Defaults to 0.2.
        verbose (bool, optional): Whether to print verbose output. Defaults to False.
    """

    RETURN_NAMES = ['var_g1', 'var_g2', 'var_gC', 'additive_collab_explv', 'additive_collab_cov', 'interactive_collab']

    def __init__(self, df, target, learner, test_size=0.2, verbose=False) -> None:
        self.df = df
        self.target = target
        self.fs = [col for col in df.columns if col != target]
        self.test_size = test_size
        self.X_train, self.X_test, self.y_train, self.y_test = train_test_split(df[self.fs], df[target], test_size=test_size)
        self.verbose = verbose
        self.decomps = {}
        self.Learner = learner
        self.models = {}
        
    def new_split(self, test_size=None):
            """
            Splits the dataset into training and testing sets.

            Args:
                test_size (float, optional): The proportion of the dataset to include in the test split.
                    If not specified, the default test size defined in the class will be used.

            Returns:
                None

            Raises:
                None
            """
            if test_size is None:
                test_size = self.test_size
            else:
                self.test_size = test_size
            self.X_train, self.X_test, self.y_train, self.y_test = train_test_split(self.df[self.fs], self.df[self.target],
                                                                                    test_size=test_size)
            self.decomps.clear()
                    
    @staticmethod
    def __sort_comb(comb, inner_only=False):
        """
        Sorts the combinations in the given `comb` tuple.

        Args:
            comb (list): The list of combinations to be sorted.
            inner_only (bool, optional): If True, only the inner elements will be sorted. 
                If False, first the inner elements, then the outer tuple will be sorted..

        Returns:
            tuple: The sorted combinations.
        """
        # TODO accomodate for a conditioning group, make sure that the conditioning is always last but the rest is sorted
        comb_s = tuple(tuple(sorted(c)) for c in comb)
        if not inner_only:
            comb_s = tuple(sorted(comb_s, key=lambda x: (len(x), x)))
            comb_s = tuple(tuple(gr) for gr in comb_s)
        return comb_s
    
    @staticmethod
    def __make_tuple(comb):
        return tuple([tuple(gr) for gr in comb])

    @staticmethod
    def __adjust_order(comb, res):
        if CollabExplainer.__sort_comb(comb, inner_only=True) == CollabExplainer.__sort_comb(comb, inner_only=False):
            return res
        else:
            res_s = res.rename({CollabExplainer.RETURN_NAMES[0]: CollabExplainer.RETURN_NAMES[1],
                                CollabExplainer.RETURN_NAMES[1]: CollabExplainer.RETURN_NAMES[0]}, inplace=False).copy()
            res_s = res_s.loc[res.index]
            return res_s
        
    @staticmethod
    def __get_terms(fs, order, exclude=None, blocked_fs=None):
        """
        Gets all possible terms of a given order for a set of features.
        with exclude specific terms can be excluded, with blocked_fs features can be 
        excluded from the set of features.
        """
        if exclude is None:
            exclude = []
        if blocked_fs is None:
            blocked_fs = []
        
        fs_wo_block = [f for f in fs if f not in blocked_fs]
        fs_s = sorted(fs_wo_block)
        terms = sum([list(itertools.combinations(fs_s, d)) for d in range(1, order+1)], [])
        terms = [term for term in terms if term not in exclude]
        # if order >= 1:
        #     terms += fs
        return terms
    
    @staticmethod
    def __get_excluded_terms(comb, order, C=None, blocked_fs=None):
        """
        Get the terms that are not in the combination.
        """
        if C is None:
            C = []
        if blocked_fs is None:
            blocked_fs = []
            
        C_l = list(C)
        termss = [CollabExplainer.__get_terms(elem + C_l, order, blocked_fs=blocked_fs) for elem in comb]
        allowed_terms = list(itertools.chain(*termss))
        fs = [f for gr in comb for f in gr] + C_l
        all_terms = CollabExplainer.__get_terms(fs, order)
        return [term for term in all_terms if term not in allowed_terms]
    
    @staticmethod
    def __get_interaction_terms_involving(fs, comb, order, C=None):
        if C is None:
            C = []
        interaction_terms = CollabExplainer.__get_excluded_terms(comb, order, C=C)
        int_terms_involving_fs = [term for term in interaction_terms if any([f in term for f in fs])]
        return int_terms_involving_fs
        
    def __get_model(self, comb, order, C=None, excluded_terms=None, blocked_fs=None):
        """
        The comb tuple of tuples indicates which groups of features are allowed to interact.
        The order specifies the max order of interactions.
        So if we get a tuple with one tuple containing all features, we get the full model of order òrder`.
        If we get a tuple with two tuples, we get the model of order `order` with the 
        interactions within the groups only.
        The `C` parameter can be used to specify a set of features that we assume to be "known" before,
        i.e. we fit the model on the residual of the best model with the features C.
        Those features and all interactions involving the features can also be used by the model.
        """
        if C is None:
            C = []
        if excluded_terms is None:
            excluded_terms = []
        if blocked_fs is None:
            blocked_fs = []
        
        # add conditioning set
        comb_s = CollabExplainer.__sort_comb(comb)
        C_s = sorted(list(C))
        fs = [f for gr in comb for f in gr]
        fs_full = fs + C
    
        excluded_terms_s = CollabExplainer.__sort_comb(excluded_terms, inner_only=False)
                
        key = (order, comb_s, tuple(C_s), tuple(excluded_terms_s), tuple(sorted(blocked_fs)))
        if key in self.models.keys():
            logging.debug(f'Using precomputed model for {comb_s}')
            return self.models[key]
        else:
            logging.debug(f'Fitting model for {comb_s}')
            
            # regress out conditioning set if nonempty
            if len(C_s) > 0:
                model_C = self.__get_model([C_s], order, C=[])
                model_C_pred_train = model_C.predict(self.X_train.loc[:, C_s])
                y_res_C_train = self.y_train - model_C_pred_train
            else:
                y_res_C_train = self.y_train
                
            # add interactions between groups to the list of excluded terms
            if len(comb_s) > 1:
                excluded_terms += CollabExplainer.__get_excluded_terms(comb, order, C=C,
                                                                       blocked_fs=blocked_fs)
            # fit model
            if len(excluded_terms) == 0:
                model = self.Learner(exclude=None)
            else:
                model = self.Learner(exclude=excluded_terms)
            model.fit(self.X_train.loc[:, fs_full], y_res_C_train)
            
            # store model and return result
            self.models[key] = model
            return model
                
    def __assert_comb_valid(self, comb, C=None):
        """
        Asserts that the combination contains two elements, that the features are in the columns, that the 
        two sets are disjoint. If an element is a string, it is converted to a list, such that always a list
        of two lists is returned.
        """
        if C is None:
            C = []
        assert len(comb) == 2, 'Please provide exactly two sets of features'
        comb_ = list(comb)
        for i in range(len(comb_)):
            if isinstance(comb_[i], str):
                comb_[i] = [comb_[i]]
            elif isinstance(comb_[i], tuple):
                comb_[i] = list(comb_[i])
            else:
                assert isinstance(comb_[i], list), 'The elements of the combination must be strings or lists'
            assert all([f in self.fs for f in comb_[i]]), 'Feature not in the dataset'
        assert len(set(comb_[0]).intersection(set(comb_[1]))) == 0, 'the two sets of features must be disjoint'
        assert len(set(comb_[0]).union(set(comb_[1])).intersection(set(C))) == 0, 'the conditioning set must be disjoint from the two sets'
        return comb_
                    
    def get(self, comb, order=2, C=None, block_int=None, block_add=None):
        if C is None:
            C = []
        if block_int is None:
            block_int = []
        if block_add is None:
            block_add = []
        
        comb = self.__assert_comb_valid(comb)
        comb_s = CollabExplainer.__sort_comb(comb)
        key = (comb_s, tuple(sorted(C)), tuple(sorted(block_int)), tuple(sorted(block_add)))
        if key in self.decomps.keys():
            res = self.decomps[key]
            return self.__adjust_order(comb, res)
        else:
            res = self.__compute(list(comb_s), order=order, C=C, block_int=block_int, block_add=block_add)
            self.decomps[key] = res
            return res
        
    def __compute(self, comb, order=2, C=None, block_int=None, block_add=None):
        """
        Computes decomposition for a combination comb conditional on
        a group of features C. Uses GAMs of at most order `order`
        to compute the decomposition.
        
        Args:
            comb (list): A list of two lists of features.
            order (int, optional): The maximum order of interactions. Defaults to 2.
            C (list, optional): A list of features that are assumed to be known. Defaults to [].
        """
        if C is None:
            C = []
        if block_int is None:
            block_int = []
        if block_add is None:
            block_add = []
        
        comb = self.__assert_comb_valid(comb)
        return_names = self.RETURN_NAMES
        
        # comb = [f for gr in comb for f in gr]
        fs = [f for gr in comb for f in gr]
        fs_full = fs + C
        fs_0 = comb[0] + C
        fs_1 = comb[1] + C
        
        # get baseline
        if len(C) > 0:
            fC = self.__get_model([C], order, C=[])
            fC_pred_test = fC.predict(self.X_test.loc[:, C])
        else:
            fC_pred_test = np.repeat(0, self.y_test.shape)
        
        v_f_empty = mean_squared_error(self.y_test, np.repeat(np.mean(self.y_train), self.y_test.shape))
        v_fC = v_f_empty - mean_squared_error(self.y_test, fC_pred_test)
                
        f = self.__get_model([fs], order, C=C)
        f_GAM = self.__get_model(comb, order, C=C)
        f1 = self.__get_model([comb[0]], order, C=C)
        f2 = self.__get_model([comb[1]], order, C=C)
        
        v_f = v_f_empty - mean_squared_error(self.y_test, f.predict(self.X_test[fs_full]) + fC_pred_test)
        v_f_GAM = v_f_empty - mean_squared_error(self.y_test, f_GAM.predict(self.X_test[fs_full]) + fC_pred_test)
        v_f1 = v_f_empty - mean_squared_error(self.y_test, f1.predict(self.X_test[fs_0]) + fC_pred_test)
        v_f2 = v_f_empty - mean_squared_error(self.y_test, f2.predict(self.X_test[fs_1]) + fC_pred_test)

        # get the GAM components
        terms_C = self.__get_terms(C, order)
        terms_g1 = self.__get_terms(fs_0, order, exclude=terms_C)
        terms_g2 = self.__get_terms(fs_1, order, exclude=terms_C)
                
        # get the GAM predictions on test data
        g1_test = f_GAM.predict_components(self.X_test, terms_g1)
        g2_test = f_GAM.predict_components(self.X_test, terms_g2)
        
        if len(block_int) > 0:
            excluded_ints = CollabExplainer.__get_interaction_terms_involving(block_int, comb, order, C=C)
            f_wo_blocked_int = self.__get_model([fs], order, C=C, excluded_terms=excluded_ints)
            v_f_wo_blocked_int = v_f_empty - mean_squared_error(self.y_test,
                                                                f_wo_blocked_int.predict(self.X_test[fs_full]) + fC_pred_test)
        if len(block_add) > 0:
            f_GAM_wo_blocked_add = self.__get_model(comb, order, C=C, blocked_fs=block_add, excluded_terms=[])
            v_f_GAM_wo_blocked_add = v_f_empty - mean_squared_error(self.y_test,
                                                                    f_GAM_wo_blocked_add.predict(self.X_test[fs_full]) + fC_pred_test)
            g1_blocked_test = f_GAM_wo_blocked_add.predict_components(self.X_test, terms_g1)
            g2_blocked_test = f_GAM_wo_blocked_add.predict_components(self.X_test, terms_g2)        
        
        # if C is not empty, we make the GAM components orthogonal to C to recover uniquness
        if len(C) > 0:
            g1_train = f_GAM.predict_components(self.X_train, terms_g1)
            g2_train = f_GAM.predict_components(self.X_train, terms_g2)
            
            # regressing X_C out of g1
            model_g1 = self.Learner()
            model_g1.fit(self.X_train[C], g1_train)
            g1_pred = model_g1.predict(self.X_test[C])
            g1_res = g1_test - g1_pred
            
            # regressing X_C out of g2
            model_g2 = self.Learner()
            model_g2.fit(self.X_train[C], g2_train)
            g2_pred = model_g2.predict(self.X_test[C])
            g2_res = g2_test - g2_pred
            
            gc_test = f_GAM.predict_components(self.X_test, terms_C)
            var_comp = np.var(g1_pred + g2_pred + gc_test) / np.var(g1_test + g2_test + gc_test)
            logging.debug(f'Variance of GAM explained by X_C: {var_comp}')
        else:
            g1_res = g1_test
            g2_res = g2_test
        
        # compute collaboration scores
        cov_g1_g2 = np.cov(g1_res, g2_res)[0, 1]
        additive_collab = v_f_GAM - v_f1 - v_f2 + v_fC
        additive_collab_wo_cov = additive_collab + 2*cov_g1_g2            
        interactive_collab = v_f - v_f_GAM
        
        # TODO move this up such that it also works if C is nonempty
        # compute interactive collab if there are blocked interactions
        if len(block_int) > 0:
            interactive_collab = v_f_wo_blocked_int - v_f_GAM
            
        # compute additive collab if there are blocked additive terms
        if len(block_add) > 0:
            additive_collab = v_f_GAM_wo_blocked_add - v_f1 - v_f2 + v_fC
            cov_g1_g2 = np.cov(g1_blocked_test, g2_blocked_test)[0, 1]
            additive_collab_wo_cov = additive_collab + 2*cov_g1_g2
        # end TODO area    
        

        if self.verbose:
            print(f'comb: {comb}, C: {C}')
            print(f'v(comb + C): {v_f} \n v(C): {v_fC} \n  v(comb[0] + C): {v_f1} \n v(comb[1] + C): {v_f2}')
            print(f'Additive Collaboration: {additive_collab} \n Interactive Collaboration: {interactive_collab}')
            print(f'Additive wo Cov: {additive_collab_wo_cov} \n -2*cov(g1, g2): {-2*cov_g1_g2}')
                     
        # rescale to proportion of variance of Y 
        var_y = np.var(self.y_test)
        factor = 1 / var_y
        v_f1 *= factor
        v_f2 *= factor
        additive_collab *= factor
        additive_collab_wo_cov *= factor
        cov_g1_g2 *= factor
        interactive_collab *= factor
        v_fC *= factor
                   
        return pd.Series([v_f1, v_f2, v_fC, additive_collab_wo_cov, -2*cov_g1_g2, interactive_collab], index=return_names) 
        
    def get_all_pairwise(self, only_precomputed=False, return_matrixs=False):
        '''
        Gives a detailed decomposition of all features respecting interactions and the dependencies between them

        Parameters:
            data: A datasetcontaining all the data
        '''
        logging.info('Computing all decompositions')
        if only_precomputed:
            combinations = list(self.decomps.keys())
        else:
            combinations = [list(comb) for comb in itertools.combinations(self.fs, 2)]
        
        if return_matrixs:
            vars_bivarivate = pd.DataFrame(index=self.fs, columns=self.fs, dtype=float)
            additive_collab = pd.DataFrame(index=self.fs, columns=self.fs, dtype=float)
            neg2_cov_g1_g2 = pd.DataFrame(index=self.fs, columns=self.fs, dtype=float)
            additive_collab_wo_cov = pd.DataFrame(index=self.fs, columns=self.fs, dtype=float)
            synergetic_collab = pd.DataFrame(index=self.fs, columns=self.fs, dtype=float)
            
            for comb in tqdm.tqdm(combinations):
                res = self.get(comb)
                # hacky but works
                vars_bivarivate.loc[comb[0], comb[0]] = res[self.RETURN_NAMES[0]]
                vars_bivarivate.loc[comb[1], comb[1]] = res[self.RETURN_NAMES[1]]  
                # rest                              
                vars_bivarivate.loc[comb[0], comb[1]] = res.sum(axis=0)
                additive_collab.loc[comb[0], comb[1]] = res[self.RETURN_NAMES[3]] + res[self.RETURN_NAMES[4]]
                neg2_cov_g1_g2.loc[comb[0], comb[1]] = res[self.RETURN_NAMES[4]]
                additive_collab_wo_cov.loc[comb[0], comb[1]] = res[self.RETURN_NAMES[3]]
                synergetic_collab.loc[comb[0], comb[1]] = res[self.RETURN_NAMES[5]]
                # make symmetric
                vars_bivarivate.loc[comb[1], comb[0]] = vars_bivarivate.loc[comb[0], comb[1]]
                additive_collab.loc[comb[1], comb[0]] = additive_collab.loc[comb[0], comb[1]]
                neg2_cov_g1_g2.loc[comb[1], comb[0]] = neg2_cov_g1_g2.loc[comb[0], comb[1]]
                additive_collab_wo_cov.loc[comb[1], comb[0]] = additive_collab_wo_cov.loc[comb[0], comb[1]]
                synergetic_collab.loc[comb[1], comb[0]] = synergetic_collab.loc[comb[0], comb[1]]
            
            return vars_bivarivate, additive_collab, synergetic_collab, neg2_cov_g1_g2, additive_collab_wo_cov
        else:                
            results = pd.DataFrame(combinations, columns=['feature1', 'feature2'])
            results.set_index(['feature1', 'feature2'], inplace=True)
            for comb in tqdm.tqdm(combinations):
                res = self.get(comb)
                results.loc[tuple(comb), res.index] = res
                res2 = self.get(comb[::-1])
                results.loc[tuple(comb[::-1]), res2.index] = res2
            ex = Explanation('all pairwise', results)
            return ex
        
    def get_all_pairwise_onefixed(self, feature):
        '''
        Gives a detailed decomposition of all features respecting interactions and the dependencies between them

        Parameters:
            data: A datasetcontaining all the data
        '''
        logging.info(f'Computing all decompositions for feature {feature}')
        combinations = [[feature, col] for col in self.fs if col != feature]
        results = pd.DataFrame(combinations, columns=['feature1', 'feature2'])
        results.set_index(['feature1', 'feature2'], inplace=True)
        for comb in tqdm.tqdm(combinations):
            res = self.get(comb)
            results.loc[tuple(comb), res.index] = res
            res_flip = res.rename({self.RETURN_NAMES[0]: self.RETURN_NAMES[1],
                                   self.RETURN_NAMES[1]: self.RETURN_NAMES[0]})
            results.loc[tuple(comb[::-1]), res_flip.index] = res_flip
        ex = SurplusExplanation(f'{feature} vs j', results, feature)
        return ex
    
    def get_loo(self, feature):
        """
        Computes one vs rest decomposition for a given feature
        """
        rest = [col for col in self.fs if col != feature]
        res = self.get([feature, rest])
        ex = SurplusExplanation(f'{feature} vs rest', res)
        return ex
    
    def get_all_loo(self):
        """
        Computes one vs rest decomposition for all features
        """
        results = pd.DataFrame(index=self.fs, columns=self.RETURN_NAMES)
        for feature in tqdm.tqdm(self.fs):
            results.loc[feature] = self.get_loo(feature).scores
        return SurplusExplanation('one vs rest', results)
    
    def get_pairs_cond_rest(self, fixed_feature):
        """
        For a fixed feature, computes pairwise decompositions conditional on the
        respective remainder.
        """
        rest = [f for f in self.fs if f != fixed_feature]
        results = pd.DataFrame(index=rest, columns=self.RETURN_NAMES)
        for feature in tqdm.tqdm(rest):
            C = [f for f in rest if f != feature]
            results.loc[feature] = self.get([[fixed_feature], [feature]], C=C)
        ex = CollabExplanation(f'{fixed_feature} vs j | rest', results, feature)
        return ex
    
    def get_loo_cond_one(self, fixed_feature):
        rest = [f for f in self.fs if f != fixed_feature]
        one_vs_rest = self.get([fixed_feature, rest])
        results = pd.DataFrame(index=rest, columns=self.RETURN_NAMES)
        for feature in tqdm.tqdm(rest):
            R = [f for f in rest if f != feature]
            if len(R) == 0:
                raise ValueError('The rest set must contain at least one feature')
            else:
                results.loc[feature] = one_vs_rest - self.get([fixed_feature, R], C=[feature])
        ex = CollabExplanation(f'({fixed_feature} vs rest) - ({fixed_feature} vs rest | j)', results, feature)
        return ex