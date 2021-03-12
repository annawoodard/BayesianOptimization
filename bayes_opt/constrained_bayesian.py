import warnings

from .target_space import TargetSpace
from .event import Events, DEFAULT_EVENTS
from .logger import _get_default_logger
from .util import UtilityFunction, acq_max, ensure_rng

from sklearn.gaussian_process.kernels import Matern, WhiteKernel
from sklearn.gaussian_process import GaussianProcessRegressor


class Queue:
    def __init__(self):
        self._queue = []

    @property
    def empty(self):
        return len(self) == 0

    def __len__(self):
        return len(self._queue)

    def __next__(self):
        if self.empty:
            raise StopIteration("Queue is empty, no more objects to retrieve.")
        obj = self._queue[0]
        self._queue = self._queue[1:]
        return obj

    def next(self):
        return self.__next__()

    def add(self, obj):
        """Add object to end of queue."""
        self._queue.append(obj)


class Observable(object):
    """

    Inspired/Taken from
        https://www.protechtraining.com/blog/post/879#simple-observer
    """

    def __init__(self, events):
        # maps event names to subscribers
        # str -> dict
        self._events = {event: dict() for event in events}

    def get_subscribers(self, event):
        return self._events[event]

    def subscribe(self, event, subscriber, callback=None):
        if callback is None:
            callback = getattr(subscriber, "update")
        self.get_subscribers(event)[subscriber] = callback

    def unsubscribe(self, event, subscriber):
        del self.get_subscribers(event)[subscriber]

    def dispatch(self, event):
        for _, callback in self.get_subscribers(event).items():
            callback(event, self)



from .utility import UtilityFunction, acq_max, ensure_rng
class ConstrainedBayesianOptimization(Observable):
    """
    This class takes the function to optimize as well as the parameters bounds
    in order to find which values for the parameters yield the maximum value
    using bayesian optimization.

    Parameters
    ----------
    f: function
        Function to be maximized.

    pbounds: dict
        Dictionary with parameters names as keys and a tuple with minimum
        and maximum values.

    random_state: int or numpy.random.RandomState, optional(default=None)
        If the value is an integer, it is used as the seed for creating a
        numpy.random.RandomState. Otherwise the random state provieded it is used.
        When set to None, an unseeded random state is generated.

    verbose: int, optional(default=2)
        The level of verbosity.

    bounds_transformer: DomainTransformer, optional(default=None)
        If provided, the transformation is applied to the bounds.

    Methods
    -------
    probe()
        Evaluates the function on the given points.
        Can be used to guide the optimizer.

    maximize()
        Tries to find the parameters that yield the maximum value for the
        given function.

    set_bounds()
        Allows changing the lower and upper searching bounds
    """

    def __init__(
        self, f, pbounds, random_state=None, verbose=2, bounds_transformer=None, tags=None
    ):
        if tags is None:
            self._tags = ['objective', 'constraint']

        self._random_state = ensure_rng(random_state)

        self._queue = Queue()

        self._verbose = verbose
        self._bounds_transformer = bounds_transformer

        self._space = {}
        self._gp = {}
        self._bounds_transformer = {}
        for tag in self._tags:
            # Data structure containing the function to be optimized, the bounds of
            # its domain, and a record of the evaluations we have done so far
            self._space[tag] = TargetSpace(f, pbounds, random_state)


            # Internal GP regressor
            self._gp[tag] = GaussianProcessRegressor(
                kernel=Matern(nu=2.5) + WhiteKernel(noise_level=1e-3),
                alpha=1e1,
                normalize_y=True,
                n_restarts_optimizer=5,
                random_state=self._random_state,
            )

        if bounds_transformer:
            self.transformer_tag, self.bounds_transformer = bounds_transformer
            try:
                self.bounds_transformer.initialize(self._space[self.transformer_tag])
            except (AttributeError, TypeError) as e:
                raise TypeError(
                    "The transformer must be an instance of " "DomainTransformer"
                )

        super().__init__(events=DEFAULT_EVENTS)

    @property
    def space(self):
        return self._space

    @property
    def max(self, tag):
        return self._space[tag].max()

    @property
    def res(self, tag):
        return self._space[tag].res()

    def register(self, tag, params, target):
        """Expect observation with known target"""
        self._space[tag].register(params, target)
        self.dispatch(Events.OPTIMIZATION_STEP)
        if self._bounds_transformer:
            if self._transformer_tag == tag:
                new_bounds = self._bounds_transformer.transform(self._space[tag])
                self.set_bounds(new_bounds)

    def probe(self, tag, params, lazy=True):
        """
        Evaluates the function on the given points. Useful to guide the optimizer.

        Parameters
        ----------
        params: dict or list
            The parameters where the optimizer will evaluate the function.

        lazy: bool, optional(default=True)
            If True, the optimizer will evaluate the points when calling
            maximize(). Otherwise it will evaluate it at the moment.
        """
        if lazy:
            self._queue.add(params)
        else:
            self._space[tag].probe(params)
            self.dispatch(Events.OPTIMIZATION_STEP)

    def suggest(self, utility_function):
        """Most promising point to probe next"""
        space = self._space[self._tags[0]]
        if len(space) == 0:
            return space.array_to_params(space.random_sample())

        # Sklearn's GP throws a large number of warnings at times, but
        # we don't really need to see them here.
        # for tag in self._tags:
        #     self._gp[tag].fit(self._space[tag].params, self._space[tag].target)
        # with warnings.catch_warnings():
        #     warnings.simplefilter("ignore")
        #     for tag in self._tags:
        #         self._gp[tag].fit(self._space[tag].params, self._space[tag].target)
        for tag in self._tags:
            self._gp[tag].fit(self._space[tag].params, self._space[tag].target)

        # Finding argmax of the acquisition function.
        space = self._space[self._tags[0]]
        suggestion = acq_max(
            ac=utility_function,
            gp=self._gp,
            bounds=space.bounds,
            random_state=self._random_state,
        )

        return space.array_to_params(suggestion)

    def _prime_queue(self, init_points):
        """Make sure there's something in the queue at the very beginning."""
        space = self._space[self._tags[0]]
        if self._queue.empty and space.empty:
            init_points = max(init_points, 1)

        for _ in range(init_points):
            self._queue.add(space.random_sample())

    def maximize(
        self,
        init_points=5,
        n_iter=25,
        acq="ucb",
        kappa=2.576,
        kappa_decay=1,
        kappa_decay_delay=0,
        xi=0.0,
        gp_params=None
    ):
        """
        Probes the target space to find the parameters that yield the maximum
        value for the given function.

        Parameters
        ----------
        init_points : int, optional(default=5)
            Number of iterations before the explorations starts the exploration
            for the maximum.

        n_iter: int, optional(default=25)
            Number of iterations where the method attempts to find the maximum
            value.

        acq: {'ucb', 'ei', 'poi'}
            The acquisition method used.
                * 'ucb' stands for the Upper Confidence Bounds method
                * 'ei' is the Expected Improvement method
                * 'poi' is the Probability Of Improvement criterion.

        kappa: float, optional(default=2.576)
            Parameter to indicate how closed are the next parameters sampled.
                Higher value = favors spaces that are least explored.
                Lower value = favors spaces where the regression function is the
                highest.

        kappa_decay: float, optional(default=1)
            `kappa` is multiplied by this factor every iteration.

        kappa_decay_delay: int, optional(default=0)
            Number of iterations that must have passed before applying the decay
            to `kappa`.

        xi: float, optional(default=0.0)
            [unused]
        """
        # TODO
        raise NotImplementedError
        self._prime_subscriptions()
        self.dispatch(Events.OPTIMIZATION_START)
        self._prime_queue(init_points)
        if gp_params is not None:
            for tag, params in gp_params.items():
                self.set_gp_params(tag, **params)


        util = UtilityFunction(
            kind=acq,
            kappa=kappa,
            xi=xi,
            kappa_decay=kappa_decay,
            kappa_decay_delay=kappa_decay_delay,
        )
        iteration = 0
        while not self._queue.empty or iteration < n_iter:
            try:
                x_probe = next(self._queue)
            except StopIteration:
                util.update_params()
                x_probe = self.suggest(util)
                iteration += 1

            self.probe(x_probe, lazy=False)

            if self._bounds_transformer:
                self.set_bounds(self._bounds_transformer.transform(self._space))

        self.dispatch(Events.OPTIMIZATION_END)

    def set_bounds(self, new_bounds):
        """
        A method that allows changing the lower and upper searching bounds

        Parameters
        ----------
        new_bounds : dict
            A dictionary with the parameter name and its new bounds
        """
        for tag in self._tags:
            self._space[tag].set_bounds(new_bounds)

    def set_gp_params(self, tag, **params):
        """Set parameters to the internal Gaussian Process Regressor"""
        self._gp[tag].set_params(**params)


