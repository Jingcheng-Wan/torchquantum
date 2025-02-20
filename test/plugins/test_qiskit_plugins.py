from qiskit import QuantumCircuit
import numpy as np
import random
from qiskit.opflow import StateFn, X, Y, Z, I

import torchquantum as tq

from torchquantum.plugins import op_history2qiskit, QiskitProcessor
from torchquantum.utils import switch_little_big_endian_state

import torch

pauli_str_op_dict = {
    "X": X,
    "Y": Y,
    "Z": Z,
    "I": I,
}


def test_expval_observable():
    # seed = 0
    # random.seed(seed)
    # np.random.seed(seed)
    # torch.manual_seed(seed)
    processor = QiskitProcessor(use_real_qc=False, n_shots=100000)

    for k in range(10):
        # print(k)
        n_wires = random.randint(1, 4)
        obs = random.choices(["X", "Y", "Z", "I"], k=n_wires)
        random_layer = tq.RandomLayer(n_ops=100, wires=list(range(n_wires)))
        qdev = tq.QuantumDevice(n_wires=n_wires, bsz=1, record_op=True)
        random_layer(qdev)
        qiskit_circ = op_history2qiskit(qdev.n_wires, qdev.op_history)

        expval_qiskit_processor = processor.process_circs_get_joint_expval([qiskit_circ], "".join(obs))

        operator = pauli_str_op_dict[obs[0]]
        for ob in obs[1:]:
            # note here the order is reversed because qiskit is in little endian
            operator = pauli_str_op_dict[ob] ^ operator
        psi = StateFn(qiskit_circ)
        psi_evaled = psi.eval()._primitive._data
        state_tq = switch_little_big_endian_state(
            qdev.get_states_1d().detach().numpy()
        )[0]
        assert np.allclose(psi_evaled, state_tq, atol=1e-5)

        expval_qiskit = (~psi @ operator @ psi).eval().real
        # print(expval_qiskit_processor, expval_qiskit)
        if n_wires <= 3: # if too many wires, the stochastic method is not accurate due to limited shots
            assert np.isclose(expval_qiskit_processor, expval_qiskit, atol=1e-2)

    print("expval observable test passed")


if __name__ == '__main__':
    test_expval_observable()
