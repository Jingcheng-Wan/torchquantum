import torch
import torch.nn.functional as F
import torch.optim as optim
import argparse

import torchquantum as tq
import torchquantum.functional as tqf
from torchquantum.datasets import MNIST
from torch.optim.lr_scheduler import CosineAnnealingLR
from torchquantum.plugins import (
    tq2qiskit_expand_params,
    tq2qiskit,
    qiskit2tq,
    tq2qiskit_measurement,
    qiskit_assemble_circs,
    op_history2qiskit,
    op_history2qiskit_expand_params,
)
from torchquantum.utils import (
    build_module_from_op_list,
    build_module_op_list,
    get_v_c_reg_mapping,
    get_p_c_reg_mapping,
    get_p_v_reg_mapping,
    get_cared_configs,
)

from torchquantum.plugins import QiskitProcessor

import random
import numpy as np


class QFCModel(tq.QuantumModule):
    class QLayer(tq.QuantumModule):
        def __init__(self):
            super().__init__()
            self.n_wires = 4
            self.random_layer = tq.RandomLayer(n_ops=50, wires=list(range(self.n_wires)))
            # self.arch = {'n_wires': self.n_wires, 'n_blocks': 4, 'n_layers_per_block': 2}
            # self.random_layer = tq.layers.U3CU3Layer0(self.arch)

            # gates with trainable parameters
            self.rx0 = tq.RX(has_params=True, trainable=True)
            self.ry0 = tq.RY(has_params=True, trainable=True)
            self.rz0 = tq.RZ(has_params=True, trainable=True)
            self.crx0 = tq.CRX(has_params=True, trainable=True)

        @tq.static_support
        def forward(self, qdev: tq.QuantumDevice):
            """
            1. To convert tq QuantumModule to qiskit or run in the static
            model, need to:
                (1) add @tq.static_support before the forward
                (2) make sure to add
                    static=self.static_mode and
                    parent_graph=self.graph
                    to all the tqf functions, such as tqf.hadamard below
            """
            self.random_layer(qdev)

            # some trainable gates (instantiated ahead of time)
            self.rx0(qdev, wires=0)
            self.ry0(qdev, wires=1)
            self.rz0(qdev, wires=3)
            self.crx0(qdev, wires=[0, 2])

            # add some more non-parameterized gates (add on-the-fly)
            tqf.hadamard(qdev, wires=3, static=self.static_mode, parent_graph=self.graph)
            tqf.sx(qdev, wires=2, static=self.static_mode, parent_graph=self.graph)
            tqf.cnot(qdev, wires=[3, 0], static=self.static_mode, parent_graph=self.graph)

    def __init__(self):
        super().__init__()
        self.n_wires = 4
        self.encoder = tq.GeneralEncoder(tq.encoder_op_list_name_dict["4x4_ryzxy"])

        self.q_layer = self.QLayer()
        self.measure = tq.MeasureAll(tq.PauliZ)

    def forward(self, x, use_qiskit=False):
        qdev = tq.QuantumDevice(n_wires=self.n_wires, bsz=x.shape[0], device=x.device, record_op=True)
        bsz = x.shape[0]
        x = F.avg_pool2d(x, 6).view(bsz, 16)
        devi = x.device

        if use_qiskit:
            self.encoder(qdev, x)
            op_history_parameterized = qdev.op_history
            qdev.reset_op_history()
            encoder_circ = op_history2qiskit_expand_params(self.n_wires, op_history_parameterized, bsz=bsz)
            self.q_layer(qdev)
            op_history_fixed = qdev.op_history
            qdev.reset_op_history()
            q_layer_circ = op_history2qiskit(self.n_wires, op_history_fixed)
            measurement_circ = tq2qiskit_measurement(qdev, self.measure)

            assembed_circs = qiskit_assemble_circs(encoder_circ, q_layer_circ, measurement_circ)
            x = self.qiskit_processor.process_ready_circs(qdev, assembed_circs).to(devi)
        else:
            self.encoder(qdev, x)
            self.q_layer(qdev)
            x = self.measure(qdev)

        x = x.reshape(bsz, 2, 2).sum(-1).squeeze()
        x = F.log_softmax(x, dim=1)

        return x


def train(dataflow, model, device, optimizer):
    for feed_dict in dataflow["train"]:
        inputs = feed_dict["image"].to(device)
        targets = feed_dict["digit"].to(device)

        outputs = model(inputs)
        loss = F.nll_loss(outputs, targets)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        print(f"loss: {loss.item()}", end="\r")


def valid_test(dataflow, split, model, device, qiskit=False):
    target_all = []
    output_all = []
    with torch.no_grad():
        for feed_dict in dataflow[split]:
            inputs = feed_dict["image"].to(device)
            targets = feed_dict["digit"].to(device)

            outputs = model(inputs, use_qiskit=qiskit)

            target_all.append(targets)
            output_all.append(outputs)
        target_all = torch.cat(target_all, dim=0)
        output_all = torch.cat(output_all, dim=0)

    _, indices = output_all.topk(1, dim=1)
    masks = indices.eq(target_all.view(-1, 1).expand_as(indices))
    size = target_all.shape[0]
    corrects = masks.sum().item()
    accuracy = corrects / size
    loss = F.nll_loss(output_all, target_all).item()

    print(f"{split} set accuracy: {accuracy}")
    print(f"{split} set loss: {loss}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--static", action="store_true", help="compute with " "static mode"
    )
    parser.add_argument("--pdb", action="store_true", help="debug with pdb")
    parser.add_argument(
        "--wires-per-block", type=int, default=2, help="wires per block int static mode"
    )
    parser.add_argument(
        "--epochs", type=int, default=30, help="number of training epochs"
    )

    args = parser.parse_args()

    if args.pdb:
        import pdb

        pdb.set_trace()

    seed = 0
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

    dataset = MNIST(
        root="./mnist_data",
        train_valid_split_ratio=[0.9, 0.1],
        digits_of_interest=[3, 6],
        n_test_samples=75,
    )
    dataflow = dict()

    for split in dataset:
        sampler = torch.utils.data.RandomSampler(dataset[split])
        dataflow[split] = torch.utils.data.DataLoader(
            dataset[split],
            batch_size=256,
            sampler=sampler,
            num_workers=8,
            pin_memory=True,
        )

    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")

    model = QFCModel().to(device)

    # noise_model_tq = builder.make_noise_model_tq()

    n_epochs = args.epochs

    from qiskit import IBMQ
    IBMQ.load_account()

    qdev = tq.QuantumDevice(n_wires=model.n_wires)
    circ = tq2qiskit(qdev, model.q_layer)
    """
    add measure because the transpile process may permute the wires,
    so we need to get the final q reg to c reg mapping
    """
    circ.measure_all()
    # circ.draw(output='mpl', filename='before-transpile.png')
    processor = QiskitProcessor(use_real_qc=True, backend_name="ibmq_quito")

    circ_transpiled = processor.transpile(circs=circ)
    # circ_transpiled.draw(output='mpl', filename='after-transpile.png')
    
    q_layer = qiskit2tq(circ=circ_transpiled)

    model.measure.set_v_c_reg_mapping(get_v_c_reg_mapping(circ_transpiled))
    model.q_layer = q_layer

    noise_model_tq = tq.NoiseModelTQ(
        noise_model_name="ibmq_quito",
        n_epochs=n_epochs,
        # noise_total_prob=0.5,
        # ignored_ops=configs.trainer.ignored_noise_ops,
        factor=1,
        add_thermal=True,
    )

    noise_model_tq.is_add_noise = True
    # noise_model_tq.v_c_reg_mapping = {'v2c': {0:0, 1:1, 2:2, 3:3, 4:4, 5:5, 6:6},
    #                                   'c2v': {0:0, 1:1, 2:2, 3:3, 4:4, 5:5, 6:6},
    #                                   }
    # noise_model_tq.p_c_reg_mapping = {'p2c': {0:0, 1:1, 2:2, 3:3, 4:4, 5:5, 6:6},
    #                                   'c2p': {0:0, 1:1, 2:2, 3:3, 4:4, 5:5, 6:6},
    #                                   }
    # noise_model_tq.p_v_reg_mapping ={'p2v': {0:0, 1:1, 2:2, 3:3, 4:4, 5:5, 6:6},
    #                                   'v2p': {0:0, 1:1, 2:2, 3:3, 4:4, 5:5, 6:6},
    #                                   }
    noise_model_tq.v_c_reg_mapping = get_v_c_reg_mapping(circ_transpiled)
    noise_model_tq.p_c_reg_mapping = get_p_c_reg_mapping(circ_transpiled)
    noise_model_tq.p_v_reg_mapping = get_p_v_reg_mapping(circ_transpiled)
    model.set_noise_model_tq(noise_model_tq)

    optimizer = optim.Adam(model.parameters(), lr=5e-3, weight_decay=1e-4)
    scheduler = CosineAnnealingLR(optimizer, T_max=n_epochs)

    if args.static:
        # optionally to switch to the static mode, which can bring speedup
        # on training
        model.q_layer.static_on(wires_per_block=args.wires_per_block)

    for epoch in range(1, n_epochs + 1):
        # train
        print(f"Epoch {epoch}:")
        train(dataflow, model, device, optimizer)
        print(optimizer.param_groups[0]["lr"])

        # valid
        valid_test(dataflow, "valid", model, device)
        scheduler.step()
    print(noise_model_tq.noise_counter)

    # test
    valid_test(dataflow, "test", model, device, qiskit=False)

    # run on Qiskit simulator and real Quantum Computers
    try:
        from qiskit import IBMQ

        # firstly perform simulate
        print(f"\nTest with Qiskit Simulator")
        backend_name = "ibmq_quito"
        processor_simulation = QiskitProcessor(use_real_qc=False, noise_model_name=backend_name)
        model.set_qiskit_processor(processor_simulation)
        valid_test(dataflow, "test", model, device, qiskit=True)
        # valid_test(dataflow, "valid", model, device, qiskit=True)

        # then try to run on REAL QC
        print(f"\nTest on Real Quantum Computer {backend_name}")
        processor_real_qc = QiskitProcessor(use_real_qc=True, backend_name=backend_name)
        model.set_qiskit_processor(processor_real_qc)
        valid_test(dataflow, "test", model, device, qiskit=True)
    except ImportError:
        print(
            "Please install qiskit, create an IBM Q Experience Account and "
            "save the account token according to the instruction at "
            "'https://github.com/Qiskit/qiskit-ibmq-provider', "
            "then try again."
        )


if __name__ == "__main__":
    main()