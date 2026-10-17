import numpy as np

#I've got no clue how audio works. So. Ai
class DeepFry:
    """Hard saturation, bit crushing, and sample-hold rate reduction."""

    def __init__(self, sample_rate: int, drive: float = 1.0, bit_depth: int = 16, downsample: int = 1) -> None:
        self.sample_rate = int(sample_rate)
        self.drive = 1.0
        self.bit_depth = 16
        self.downsample = 1
        self._hold_value = np.float32(0.0)
        self._hold_remaining = 0
        self.set_drive(drive)
        self.set_bit_depth(bit_depth)
        self.set_downsample(downsample)

    def set_drive(self, drive: float) -> None:
        """Set saturation amount; 1.0 is the transparent setting."""
        self.drive = min(max(float(drive), 1.0), 30.0)

    def set_bit_depth(self, bit_depth: int) -> None:
        """Set quantizer resolution from 2 through 16 bits."""
        self.bit_depth = min(max(int(bit_depth), 2), 16)

    def set_downsample(self, downsample: int) -> None:
        """Set sample-hold length; 1 is full-rate/no rate reduction."""
        self.downsample = min(max(int(downsample), 1), 32)

    def reset(self) -> None:
        self._hold_value = np.float32(0.0)
        self._hold_remaining = 0

    def process(self, mono: np.ndarray) -> np.ndarray:
        x = np.asarray(mono, dtype=np.float32)
        if x.size == 0:
            return np.zeros(0, dtype=np.float32)

        # Normalize tanh so drive=1 is exactly a passthrough.  At higher
        # drives it is a very hard, bright saturation before the crusher.
        if self.drive == 1.0:
            shaped = x
        else:
            drive = np.float32(self.drive)
            shaped = np.tanh(x * drive) / np.tanh(drive)

        # Symmetric, mid-tread quantization.  16 bits is transparent enough
        # for the off/default state; lower values add the crunchy plateaus.
        levels = float((1 << (self.bit_depth - 1)) - 1)
        crushed = np.round(shaped * levels) / levels

        if self.downsample == 1:
            out = crushed.astype(np.float32, copy=True)
        else:
            out = np.empty(crushed.shape, dtype=np.float32)
            index = 0
            size = crushed.size
            while index < size:
                if self._hold_remaining == 0:
                    self._hold_value = np.float32(crushed[index])
                    self._hold_remaining = self.downsample
                count = min(self._hold_remaining, size - index)
                out[index:index + count] = self._hold_value
                index += count
                self._hold_remaining -= count

        np.nan_to_num(out, copy=False, nan=0.0, posinf=1.0, neginf=-1.0)
        np.clip(out, -1.0, 1.0, out=out)
        return out
