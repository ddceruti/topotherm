"""Postprocessing of the results from the optimization. This includes the calculation of the
diameter and mass flow of the pipes, the elimination of unused pipes and nodes.

This module includes the following functions:
    * sts: Postprocessing for the STS model
"""

import numpy as np
import pyomo.environ as pyo
from scipy.optimize import fsolve
import networkx as nx

from topotherm.settings import Settings


def sts(model: pyo.ConcreteModel,
                matrices: dict,
                settings: Settings):
    """Create variables for the thermo-hydraulic coupled optimization.

    Args:
        model (pyo.ConcreteModel): pyomo model
        matrices (dict): dict containing the matrices
        sets (dict): dict containing the sets
        mode (str): sts or mts

    
    Returns:
        _type_: dict containing the variables
    """
    p_ij = np.array(pyo.value(model.P['ij', 'in', :, :]))
    p_ji = np.array(pyo.value(model.P['ji', 'in', :, :]))
    lambda_ij = np.array(pyo.value(model.lambda_['ij', :]))
    lambda_ji = np.array(pyo.value(model.lambda_['ji', :]))

    lambda_dir_1 = np.around(lambda_ij, 0)
    lambda_dir_2 = np.around(lambda_ji, 0)

    # Restart, Adaption of Incidence Matrix for the thermo-hydraulic coupled optimization
    for q, _ in enumerate(lambda_ij):
        if lambda_dir_1[q] == 0 and lambda_dir_2[q] == 0:
            matrices['a_i'][:, q] = 0
            matrices['l_i'][q] = 0
        elif lambda_dir_2[q] == 1:
            matrices['a_i'][:, q] = matrices['a_i'][:, q] * (-1)

    p_lin = p_ij + p_ji

    # drop entries with 0 in the incidence matrix to reduce size
    valid_columns = matrices['a_i'].any(axis=0)
    valid_rows = matrices['a_i'].any(axis=1)

    p_lin_opt = p_lin[valid_columns]
    pos_opt = matrices['position'][valid_rows, :]
    a_c_opt = matrices['a_c'][valid_rows, :]
    a_p_opt = matrices['a_p'][valid_rows, :]
    a_i_opt = matrices['a_i'][valid_rows, :][:, valid_columns]
    l_i_opt = matrices['l_i'][valid_columns]

    a_i_shape_opt = np.shape(a_i_opt)  # (rows 0, columns 1)
    d_lin2 = np.zeros(a_i_shape_opt[1])
    v_lin2 = np.zeros(a_i_shape_opt[1])
    supply_temp_opt = np.ones(a_i_shape_opt[1]) * settings.temperatures.supply
    return_temp_opt = np.ones(a_i_shape_opt[1]) * settings.temperatures.return_
    def equations(v):
        vel, d = v
        reynolds = (settings.water.density * vel * d) / settings.water.dynamic_viscosity
        f = (-1.8 * np.log10((settings.piping.roughness / (3.7 * d)) ** 1.11 + 6.9 / reynolds))**-2
        eq1 = vel - np.sqrt((2 * settings.piping.max_pr_loss * d) / (f * settings.water.density))
        eq2 = mass_lin - settings.water.density * vel * (np.pi / 4) * d ** 2
        return [eq1, eq2]

    m_lin = (p_lin_opt*1000)/(settings.water.heat_capacity_cp * (supply_temp_opt - return_temp_opt))

    for h in range(a_i_shape_opt[1]):
        mass_lin = m_lin[h]
        v_lin2[h], d_lin2[h] = fsolve(equations, (0.5, 0.02))

    res = dict(
        a_i=a_i_opt,
        a_p=a_p_opt,
        a_c=a_c_opt,
        q_c=matrices['q_c'],
        l_i=l_i_opt,
        d_i_0=d_lin2,
        m_i_0=m_lin,
        position=pos_opt,
        # reshape p_lin_opt to a 2D array with 1 column
        p=p_lin_opt
    )

    return res


def to_networkx_graph(matrices):
    """Input: matrices: a dict containíng the following keys:
        - a_i (internal matrix)
        - a_p (producer matrix)
        - a_c (consumer matrix)
        - q_c (heat demand of the consumers)
        - l_i (of the pipes)
        - positions (positions of the nodes)
        - d_i_0 (diameters of the optimal pipes)
        - m_i_0 (mass flow of the optimal pipes)
        - p (Power of the optimal pipes)
    
    Returns: Figure of the district
    """
    G = nx.DiGraph()
    s = np.array([0, 0, 0])

    # Add the nodes to the graph
    sums = matrices['a_c'].sum(axis=1)
    prod = matrices['a_p'].T.sum(axis=0)
    ges = sums + prod

    ges = np.array(ges).flatten()

    for q in range(matrices['a_c'].shape[0]):
        x, y = matrices['position'][q, 0], matrices['position'][q, 1]
        if ges[q] == 1:
            G.add_node(q, color='Red', type='consumer', x=x, y=y)
        elif ges[q] == 0:
            G.add_node(q, color='Green', type='internal', x=x, y=y)
        if ges[q] == -1:
            G.add_node(q, color='Orange', type='source', x=x, y=y)

    # edge_labels = dict()
    # Add the edges to the graph
    for k in range(matrices['a_i'].shape[1]):
        s = np.where(matrices['a_i'][:, k] == 1)[0][0], np.where(matrices['a_i'][:, k] == -1)[0][0]        
        G.add_edge(s[0], s[1],
                   weight=matrices['l_i'][k],
                   d=matrices['d_i_0'][k],
                   p=matrices['p'][k])

    # drop all edges with p=0
    G.remove_edges_from([(u, v) for u, v, d in G.edges(data=True) if d['p'] == 0])

    return G


def mts(model, matrices, sets, t_supply, t_return):
    """returns all matrices and results for further processing. Essentially,
    it simplifies the results from the optimization, including pipe diameter
    and mass flow, eliminating the unused pipes and nodes.

    Args:
        model (pyomo.environ.ConcreteModel): solved pyomo model
        matrices (dict): dict containing the incidence matrices
        sets (dict): dict containing the sets
        temperatures (dict): dict containing the supply and return temperatures
    
    Returns:
        dict: dict containing the updated matrices, including diameter and mass flow
    """
    data_dict = {}

    p_cap = np.zeros([sets['a_i_shape'][1]])
    lambda_built = np.zeros([sets['a_i_shape'][1]])
    lambda_dir = np.zeros([sets['a_i_shape'][1], len(model.set_t)])
    p_source_built = np.zeros(sets['a_p_shape'][1])

    for v in model.component_objects(pyo.Var, active=True):
        var_dict = {(v.name, index): pyo.value(v[index]) for index in v}
        data_dict.update(var_dict)
        if v.name == "lambda_built":
            for index in v:
                lambda_built[index] = pyo.value(v[index])
        if v.name == "lambda_dir_1":
            for index in v:
                lambda_dir[index] = pyo.value(v[index])
        if v.name == "P_cap":
            for index in v:
                p_cap[index] = pyo.value(v[index])
        if v.name == "P_source_cap":
            for index in v:
                p_source_built[index] = pyo.value(v[index])

    lambda_built = np.around(lambda_built, 0)
    lambda_dir = np.around(lambda_dir, 0)

    # Restart, Adaption of Incidence Matrix for the thermo-hydraulic coupled optimization
    for q, _ in enumerate(lambda_built):
        if lambda_built[q] == 0:
            matrices['a_i'][:, q] = 0
            matrices['l_i'][q] = 0
        elif (lambda_built[q] == 1) & (lambda_dir[q, 0] == 0):
            matrices['a_i'][:, q] = matrices['a_i'][:, q] * (-1)

    p_cap_opt = np.delete(p_cap, np.where(~matrices['a_i'].any(axis=0)))
    pos_opt = np.delete(matrices['position'], np.where(~matrices['a_i'].any(axis=1)), axis=0)
    a_c_opt = np.delete(matrices['a_c'], np.where(~matrices['a_i'].any(axis=1)), axis=0)
    a_p_opt = np.delete(matrices['a_p'], np.where(~matrices['a_i'].any(axis=1)), axis=0)
    a_i_opt = matrices['a_i']
    a_i_opt = np.delete(a_i_opt, np.where(~a_i_opt.any(axis=0)), axis=1)
    a_i_opt = np.delete(a_i_opt, np.where(~a_i_opt.any(axis=1)), axis=0)
    l_i_opt = matrices['l_i'][matrices['l_i'] != 0]

    a_i_shape_opt = np.shape(a_i_opt)  # (rows 0, columns 1)
    d_lin2 = np.zeros(a_i_shape_opt[1])
    v_lin2 = np.zeros(a_i_shape_opt[1])
    supply_temp_opt = np.ones(a_i_shape_opt[1]) * t_supply
    return_temp_opt = np.ones(a_i_shape_opt[1]) * t_return

    def equations(v):
        vel, d = v
        reynolds = (settings.Water.density * vel * d) / settings.Water.dynamic_viscosity
        f = (-1.8 * np.log10((settings.Piping.roughness / (3.7 * d)) ** 1.11 + 6.9 / reynolds))**-2
        eq1 = vel - np.sqrt((2 * settings.Piping.max_pr_loss * d) / (f * settings.Water.density))
        eq2 = mass_lin - settings.Water.density * vel * (np.pi / 4) * d ** 2
        return [eq1, eq2]

    m_lin = (p_cap_opt*1000)/(settings.Water.heat_capacity_cp * (supply_temp_opt - return_temp_opt))

    for h in range(a_i_shape_opt[1]):
        mass_lin = m_lin[h]
        v_lin2[h], d_lin2[h] = fsolve(equations, (0.5, 0.02))

    res = dict(
        a_i=a_i_opt,
        a_p=a_p_opt,
        a_c=a_c_opt,
        q_c=matrices['q_c'],
        l_i=l_i_opt,
        d_i_0=d_lin2,
        m_i_0=m_lin,
        position=pos_opt,
    )

    return res

