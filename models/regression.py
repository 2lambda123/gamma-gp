import sys

sys.path.append("./")

import torch
from torch.distributions import biject_to, constraints
import pyro
from pyro.distributions.util import eye_like, sum_rightmost
import pyro.distributions as dist

from models.likelihoods import log_like_np


class NPRegression:
    def __init__(self, poiss_temps, poiss_priors, ps_temps, ps_priors, data, labels_poiss, labels_ps, mask=None, f_ary=[1.0], df_rho_div_f_ary=[1.0], ps_log_priors=None, poiss_log_priors=None, subsample_size=500):
        """ Non-Poissonian regression in Pyro, without GPyTorch. Used for testing.
        """

        poiss_temps = torch.tensor(poiss_temps, dtype=torch.float64)
        self.poiss_priors = poiss_priors
        ps_temps = torch.tensor(ps_temps, dtype=torch.float64)
        self.ps_priors = ps_priors
        self.mask = torch.tensor(mask, dtype=torch.bool)
        self.f_ary = torch.tensor(f_ary, dtype=torch.float64)
        self.df_rho_div_f_ary = torch.tensor(df_rho_div_f_ary, dtype=torch.float64)
        self.data = torch.tensor(data)

        if ps_log_priors is None:
            self.ps_log_priors = torch.zeros((ps_temps.size()[0], 6))
        else:
            self.ps_log_priors = ps_log_priors

        if poiss_log_priors is None:
            self.poiss_log_priors = torch.zeros(poiss_temps.size()[0])
        else:
            self.poiss_log_priors = poiss_log_priors

        self.subsample_size = subsample_size

        self.n_poiss = len(poiss_temps)
        self.n_ps = len(ps_temps)

        self.labels_ps = labels_ps
        self.labels_poiss = labels_poiss

        self.labels_ps_params = ["A_ps", "n_1", "n_2", "n_3", "Sb_1", "Sb_2"]
        self.n_ps_params = 6

        self.n_params = self.n_poiss + self.n_ps_params * self.n_ps

        if mask is not None:
            self.poiss_temps = torch.zeros((self.n_poiss, torch.sum(~self.mask)), dtype=torch.float64)
            for i_temp in range(self.n_poiss):
                self.poiss_temps[i_temp, :] = poiss_temps[i_temp, ~self.mask]
            self.ps_temps = torch.zeros((self.n_ps, torch.sum(~self.mask)), dtype=torch.float64)
            for i_temp in range(self.n_ps):
                self.ps_temps[i_temp, :] = ps_temps[i_temp, ~self.mask]
            self.data = self.data[~self.mask]
        else:
            self.poiss_temps = poiss_temps
            self.ps_temps = ps_temps
            self.data = data

        self.init_guide = [torch.mean(self.poiss_priors[i_poiss].sample(sample_shape=(200,))) for i_poiss in torch.arange(self.n_poiss)]

        for i_ps in torch.arange(self.n_ps):
            self.init_guide += [torch.mean(self.ps_priors[i_ps][i_ps_param].sample(sample_shape=(200,))) for i_ps_param in torch.arange(self.n_ps_params)]

        self.init_guide = torch.tensor(self.init_guide)

    def guide(self):

        a_locs = pyro.param("a_locs", torch.full((self.n_params,), 0.0))
        a_scales_tril = pyro.param("a_scales", lambda: 0.1 * eye_like(a_locs, self.n_params), constraint=constraints.lower_cholesky)

        dt = dist.MultivariateNormal(a_locs, scale_tril=a_scales_tril)
        states = pyro.sample("states", dt, infer={"is_auxiliary": True})

        result = {}

        for i_poiss in torch.arange(self.n_poiss):
            transform = biject_to(self.poiss_priors[i_poiss].support)
            value = transform(states[i_poiss])
            log_density = transform.inv.log_abs_det_jacobian(value, states[i_poiss])
            log_density = sum_rightmost(log_density, log_density.dim() - value.dim() + self.poiss_priors[i_poiss].event_dim)

            result[self.labels_poiss[i_poiss]] = pyro.sample(self.labels_poiss[i_poiss], dist.Delta(value, log_density=log_density, event_dim=self.poiss_priors[i_poiss].event_dim))

        i_param = self.n_poiss

        for i_ps in torch.arange(self.n_ps):
            for i_ps_param in torch.arange(self.n_ps_params):

                transform = biject_to(self.ps_priors[i_ps][i_ps_param].support)

                value = transform(states[i_param])

                log_density = transform.inv.log_abs_det_jacobian(value, states[i_param])
                log_density = sum_rightmost(log_density, log_density.dim() - value.dim() + self.ps_priors[i_ps][i_ps_param].event_dim)

                result[self.labels_ps_params[i_ps_param] + "_" + self.labels_ps[i_ps]] = pyro.sample(self.labels_ps_params[i_ps_param] + "_" + self.labels_ps[i_ps], dist.Delta(value, log_density=log_density, event_dim=self.ps_priors[i_ps][i_ps_param].event_dim))
                i_param += 1

        return result

    def model(self):

        # Sample Poissonian parameters and compute expected contribution

        mu_poiss = torch.zeros(torch.sum(~self.mask), dtype=torch.float64)

        for i_temp in torch.arange(self.n_poiss):
            norms_poiss = pyro.sample(self.labels_poiss[i_temp], self.poiss_priors[i_temp])

            if self.poiss_log_priors[i_temp]:
                norms_poiss = 10 ** norms_poiss.clone()
            mu_poiss += norms_poiss * self.poiss_temps[i_temp]

        # Samples non-Poissonian parameters

        thetas = []

        for i_ps in torch.arange(self.n_ps):
            theta_temp = [pyro.sample(self.labels_ps_params[i_np_param] + "_" + self.labels_ps[i_ps], self.ps_priors[i_ps][i_np_param]) for i_np_param in torch.arange(self.n_ps_params)]

            for i_p in torch.arange(len(theta_temp)):
                if self.ps_log_priors[i_ps][i_p]:
                    theta_temp[i_p] = 10 ** theta_temp[i_p]

            thetas.append(theta_temp)

        # Mark each pixel as conditionally independent
        with pyro.plate("data_plate", len(self.data), dim=-1, subsample_size=self.subsample_size) as idx:

            # Use either the non-Poissonian (if there is at least one NP model) or Poissonian likelihood
            if self.n_ps != 0:
                log_likelihood = log_like_np(mu_poiss[idx], thetas, self.ps_temps[:, idx], self.data[idx], self.f_ary, self.df_rho_div_f_ary)
                pyro.factor("obs", log_likelihood)
            else:
                pyro.sample("obs", dist.Poisson(mu_poiss[idx]), obs=self.data[idx])
