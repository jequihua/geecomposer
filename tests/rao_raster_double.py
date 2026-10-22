"""Small array-backed EE test double. Not a claim of server equivalence.

Executes the production graph-building arithmetic on synthetic rasters; an
independent full ordered-pair oracle supplies expected values. Unknown methods
fail rather than returning fluent mocks. Projection is covered by real graphs.
"""

import numpy as np


class Raster:
    def __init__(self, data, mask=None, names=None):
        if isinstance(data, Raster):
            self.data, self.valid, self.names = data.data, data.valid, data.names
            return
        data = np.asarray(data, dtype=float)
        if data.ndim == 0:
            data = data.reshape(1, 1, 1)
        elif data.ndim == 1:
            data = data.reshape(1, 1, -1)
        elif data.ndim == 2:
            data = data[:, :, None]
        self.data = data
        self.valid = (
            np.ones(data.shape[:3], dtype=bool)
            if mask is None
            else np.broadcast_to(mask, data.shape[:3])
        )
        self.names = names or [f"b{i}" for i in range(data.shape[2])]

    @staticmethod
    def constant(value):
        return Raster(value)

    @staticmethod
    def cat(images):
        result = images[0]
        for image in images[1:]:
            result = result.addBands(image)
        return result

    def _op(self, other, operation):
        other = Raster(other)
        with np.errstate(all="ignore"):
            result = operation(self.data, other.data)
        return Raster(result, self.valid & other.valid, self.names)

    def add(self, other):
        return self._op(other, np.add)

    def subtract(self, other):
        return self._op(other, np.subtract)

    def multiply(self, other):
        return self._op(other, np.multiply)

    def divide(self, other):
        return self._op(other, np.divide)

    def pow(self, other):
        return self._op(other, np.power)

    def mod(self, other):
        return self._op(other, np.mod)

    def max(self, other):
        return self._op(other, np.maximum)

    def eq(self, other):
        return self._op(other, np.equal)

    def neq(self, other):
        return self._op(other, np.not_equal)

    def gt(self, other):
        return self._op(other, np.greater)

    def gte(self, other):
        return self._op(other, np.greater_equal)

    def lt(self, other):
        return self._op(other, np.less)

    def where(self, test, value):
        test, value = Raster(test), Raster(value)
        replace = test.valid & value.valid & test.data.astype(bool)
        return Raster(np.where(replace, value.data, self.data), self.valid, self.names)

    def lte(self, other):
        return self._op(other, np.less_equal)

    def And(self, other):
        return self._op(other, np.logical_and)

    def Or(self, other):
        return self._op(other, np.logical_or)

    def abs(self):
        return Raster(np.abs(self.data), self.valid, self.names)

    def floor(self):
        return Raster(np.floor(self.data), self.valid, self.names)

    def sqrt(self):
        return Raster(np.sqrt(self.data), self.valid, self.names)

    def toDouble(self):
        return self

    def reproject(self, projection):
        return self

    def clip(self, region):
        """The fixture region is a boolean mask of grid centers."""
        return self.updateMask(Raster(np.asarray(region, dtype=bool)))

    def set(self, properties):
        return self

    def select(self, bands):
        if isinstance(bands, (str, int)):
            bands = [bands]
        indices = [self.names.index(x) if isinstance(x, str) else x for x in bands]
        return Raster(
            self.data[:, :, indices],
            self.valid[:, :, indices],
            [self.names[i] for i in indices],
        )

    def rename(self, names):
        return Raster(
            self.data, self.valid, [names] if isinstance(names, str) else names
        )

    def mask(self):
        return Raster(self.valid.astype(float), names=self.names)

    def updateMask(self, mask):
        mask = Raster(mask)
        return Raster(self.data, self.valid & mask.valid & (mask.data != 0), self.names)

    def unmask(self, value, sameFootprint=True):
        return Raster(np.where(self.valid, self.data, value), names=self.names)

    def reduce(self, reducer):
        function = np.sum if reducer == "sum" else np.min
        return Raster(
            function(self.data, axis=2, keepdims=True),
            np.all(self.valid, axis=2, keepdims=True),
            ["reduced"],
        )

    def neighborhoodToArray(self, radius):
        padded = np.pad(self.data, ((radius, radius), (radius, radius), (0, 0)))
        arrays = np.lib.stride_tricks.sliding_window_view(
            padded, (2 * radius + 1, 2 * radius + 1), axis=(0, 1)
        )
        return Raster(arrays, self.valid, self.names)

    def arrayGet(self, position):
        return Raster(self.data[..., position[0], position[1]], self.valid, self.names)

    def addBands(self, other):
        other = Raster(other)
        height = max(self.data.shape[0], other.data.shape[0])
        width = max(self.data.shape[1], other.data.shape[1])

        def expand(x):
            return np.broadcast_to(x, (height, width, x.shape[2]))

        return Raster(
            np.concatenate([expand(self.data), expand(other.data)], axis=2),
            np.concatenate([expand(self.valid), expand(other.valid)], axis=2),
            self.names + other.names,
        )


class Collection:
    @staticmethod
    def fromImages(images):
        return Collection(images)

    def __init__(self, images):
        self.images = images

    def sum(self):
        result = self.images[0]
        for image in self.images[1:]:
            result = result.add(image)
        return result
