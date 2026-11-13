import unittest
from unittest import mock

from victus_fan.backends import base
from victus_fan.backends.gpu_power import GpuPower


class FakePower:
    def __init__(self, state):
        self.state = state

    def awake(self):
        return self.state


class NvidiaSmiTest(unittest.TestCase):
    def make(self, state):
        smi = base._NvidiaSmi(max_age=0, power=FakePower(state))
        smi._exe, smi._disabled = "nvidia-smi", False
        return smi

    def run_smi(self, smi):
        result = mock.Mock(returncode=0, stdout="55\n")
        with mock.patch.object(base.subprocess, "run", return_value=result) as run:
            return smi.read(), run.call_count

    def test_sleeping_gpu_is_not_woken(self):
        smi = self.make(False)
        self.assertEqual(self.run_smi(smi), (None, 0))     # nvidia-smi nicht aufgerufen
        self.assertTrue(smi.asleep)

    def test_awake_or_unknown_gpu_is_queried(self):
        for state in (True, None):
            smi = self.make(state)
            self.assertEqual(self.run_smi(smi), (55.0, 1))
            self.assertFalse(smi.asleep)

    def test_wakes_up_again(self):
        smi = self.make(False)
        self.run_smi(smi)
        smi._power.state = True
        self.assertEqual(self.run_smi(smi), (55.0, 1))
        self.assertFalse(smi.asleep)


class GpuPowerTest(unittest.TestCase):
    def test_unknown_outside_windows_or_on_error(self):
        p = GpuPower()
        p._broken = False
        with mock.patch("victus_fan.backends.gpu_power._power_states", side_effect=OSError("x"), create=True):
            self.assertIsNone(p.awake())
        self.assertIsNone(p.awake())                       # danach nicht mehr versucht

    def test_states(self):
        p = GpuPower()
        p._broken = False
        for states, expected in (([4], False), ([1], True), ([4, 1], True), ([], None), ([0], None)):
            with mock.patch("victus_fan.backends.gpu_power._power_states", return_value=states, create=True):
                self.assertEqual(p.awake(), expected, states)


if __name__ == "__main__":
    unittest.main()
