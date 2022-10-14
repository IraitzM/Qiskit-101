import random
import datetime
import dimod
import numpy as np
from cmath import pi
from qiskit import QuantumCircuit
from qiskit_finance.data_providers import *
from pyqubo import Array, Placeholder, Constraint

# For experiments
gen_seed = 42
random.seed(gen_seed)

def get_data(num_assets, seed=gen_seed):
    """ Get random data from Qiskit 

    Args:
        num_assets (int): Number of assets to be used

    Returns:
        (list, dict): Returns mu and sigma value associated with the random generator
    """
    assets = [f"TICKER{i}" for i in range(num_assets)]
    data = RandomDataProvider(tickers=assets,
                    start = datetime.datetime(2016, 1, 1),
                    end = datetime.datetime(2017, 1, 1),
                    seed = seed)
    data.run()
    mu = data.get_mean_vector() # Returns vector
    sigma = data.get_covariance_matrix() # Covariance
    return mu, sigma

def get_problem(mu, sigma):
    """
    Get the problem according to what Qiskit needs

    Args:
        mu (_type_): Average return
        sigma (_type_): Risk between assets
        q (float, optional): Risk factor Defaults to 0.5.

    Returns:
        QuadraticProblem: Problem
    """
    
    # We create our variables
    num_assets = len(mu)
    # Allowable asset allocation quantities (B_i)
    min_cost=np.min(mu)
    max_cost=np.max(mu)

    # Compute a random cost per asset and a total budget
    possible_costs=list(range(int(min_cost),int(max_cost)+1))
    costs=[random.choice(possible_costs) for _ in range(num_assets)]
    budget=sum(costs)/2

    x = Array.create('x', shape=num_assets, vartype='BINARY')

    # Profit generated by each asset individually
    H_linear_profit = 0.0
    for i in range(num_assets):
        H_linear_profit += Constraint(
            mu[i] * x[i], label='profit({})'.format(i)
        )

    # Risk obtained from the covariance matrix
    H_quadratic = 0.0
    for i in range(num_assets):
        for j in range(i + 1, num_assets):
            H_quadratic += Constraint(
                sigma[i][j] * x[i] * x[j], label='risk({}, {})'.format(i, j)
            )

    # Constraint (budget)
    H_linear_budget = 0.0
    for i in range(num_assets):
        H_linear_budget += Constraint(costs[i]*x[i], label='slot({})'.format(i))

    # Build model.
    theta1 = Placeholder('theta1')
    theta2 = Placeholder('theta2')
    theta3 = Placeholder('theta3')
    H = - theta1*H_linear_profit + theta2 * H_quadratic + theta3 * (H_linear_budget - budget)**2
    model = H.compile()
    
    # Set the Lagrange multipliers
    theta1=0.005 
    theta2=0.003
    theta3=0.005
    feed_dict = {'theta1': theta1, 'theta2' : theta2, 'theta3' : theta3}

    # Transform to QUBO.
    return model.to_qubo(feed_dict=feed_dict)
    
def get_coeffs(qubo, offset):
    
    #from QUBO to Ising Dict
    ising_coeffs=dimod.qubo_to_ising(qubo, offset=offset)

    # Order coefficients by the position they code 'x[0]' -> 0
    h = []
    jp = {}
    linear = ising_coeffs[0]
    quadratic = ising_coeffs[1]
    for i in range(len(linear)):
        h += [linear[f'x[{i}]']]
        for j in range(len(linear)):
            if (f'x[{i}]',f'x[{j}]') in quadratic:
                jp[(i,j)] = quadratic[(f'x[{i}]',f'x[{j}]')]
    return h, jp
    
def get_solution(h, jp, size):    
    def get_energy(solution):
        # Ising problem
        val = 0.0
        for i in range(len(solution)):
            val += h[i]*(solution[i]*2-1)
            
        for (i,j) in jp.keys():
                val += jp[(i,j)]*(solution[i]*2-1)*(solution[j]*2-1)
        return val
    
    e_landscape = {}
    for i in range(2**size):
        candidate = "{0:b}".format(i).zfill(size)
        e_landscape[candidate] = get_energy([int(i) for i in candidate])
    y = [y for y in e_landscape.values()]
    min_e = np.min(y)
    return min_e

# Mixer
def U_B(circ: QuantumCircuit, param): 
    for qubit in range(circ.num_qubits):
        circ.rx(param, qubit)
        
# unitary operator U_C with parameter gamma
def U_C(circ: QuantumCircuit, param, h, jp): 
    for key in jp.keys():
        q1 = key[0]
        q2 = key[1]
        circ.rzz(jp[key]*param, q1, q2)
        
    for qubit in range(circ.num_qubits):
        circ.rz(h[qubit]*param, qubit)
        
def circuit(num_assets, params, n_layers, h, jp):
    
    circ = QuantumCircuit(num_assets, num_assets)
    
    # apply Hadamards to get the n qubit |+> state
    for qubit in range(num_assets):
        circ.h(qubit)
        
    # p instances of unitary operators
    for i in range(n_layers):
        init = i*2
        U_C(circ, params[init], h, jp)
        U_B(circ, params[init+1])
        
    circ.measure(range(num_assets), range(num_assets))
    
    return circ

def compute_expectation(counts, h, jp, nshots):
    
    """
    Computes expectation value based on measurement results. It needs to be ajusted so that the distribution of 
    counts matches with the expected groud state or at least a narrow distribution against a minimal one.
    """   
    def get_energy(solution):
        # Ising problem
        val = 0.0
        for i in range(len(solution)):
            val += -h[i]*solution[i]
            
        for i in range(len(solution)):
            for j in range(i+1, len(solution)):
                val += jp[(i,j)]*solution[i]*solution[j]
        return val
    
    min_energy = 0.0
    min_e_perc = 0.0
    for key in counts: # Iterates for all counts obtained (2**n)
        solution = [int(item)*2-1 for item in key]
        output = get_energy(solution)
            
        if output < min_energy:
            min_energy = output
            min_e_perc = counts[key]/nshots
    
    # Return the value of the minimum energy found scaled to its probability
    return min_energy*min_e_perc


# Finally we write a function that executes the circuit on the chosen backend
def get_expectation(n_layers, h, jp, backend, nshots=1024):
    
    """
    Runs parametrized circuit
    """
    def execute_circ(params):
        
        qc = circuit(len(h), params, n_layers, h, jp)
        counts = backend.run(qc, seed_simulator=10, nshots=nshots).result().get_counts()
        
        return compute_expectation(counts, h, jp, nshots)
    
    return execute_circ