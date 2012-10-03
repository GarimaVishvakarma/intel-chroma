#
# ========================================================
# Copyright (c) 2012 Whamcloud, Inc.  All rights reserved.
# ========================================================


import json
from chroma_core.services.job_scheduler.dep_cache import DepCache
from collections import defaultdict

from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from chroma_core.services.job_scheduler.lock_cache import LockCache
from chroma_core.lib.job import job_log
from chroma_core.models.jobs import StateChangeJob, Command, SchedulingError, StateLock


class Transition(object):
    def __init__(self, stateful_object, old_state, new_state):
        self.stateful_object = stateful_object
        self.old_state = old_state
        self.new_state = new_state

    def __str__(self):
        return "%s/%s %s->%s" % (self.stateful_object.__class__, self.stateful_object.id, self.old_state, self.new_state)

    def __eq__(self, other):
        return (isinstance(other, self.__class__)
            and self.__dict__ == other.__dict__)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash((self.stateful_object.__hash__(), self.old_state, self.new_state))

    def to_job(self):
        job_klass = self.stateful_object.get_job_class(self.old_state, self.new_state)
        stateful_object_attr = job_klass.stateful_object
        kwargs = {stateful_object_attr: self.stateful_object, 'old_state': self.old_state}
        return job_klass(**kwargs)


class ModificationOperation(object):
    def __init__(self):
        self._dep_cache = DepCache()

    def get_expected_state(self, stateful_object_instance):
        try:
            return self.expected_states[stateful_object_instance]
        except KeyError:
            return stateful_object_instance.state

    def _create_locks(self, job):
        """Create StateLock instances based on a Job's dependencies, and
        add in any extras the job returns from Job.create_locks

        """
        locks = []
        # Take read lock on everything from job.self._dep_cache.get
        for dependency in self._dep_cache.get(job).all():
            locks.append(StateLock(
                job = job,
                locked_item = dependency.stateful_object,
                write = False
            ))

        if isinstance(job, StateChangeJob):
            stateful_object = job.get_stateful_object()
            target_klass, origins, new_state = job.state_transition

            # Take read lock on everything from get_stateful_object's self._dep_cache.get if
            # this is a StateChangeJob.  We do things depended on by both the old
            # and the new state: e.g. if we are taking a mount from unmounted->mounted
            # then we need to lock the new state's requirement of lnet_up, whereas
            # if we're going from mounted->unmounted we need to lock the old state's
            # requirement of lnet_up (to prevent someone stopping lnet while
            # we're still running)
            from itertools import chain
            for d in chain(self._dep_cache.get(stateful_object, job.old_state).all(), self._dep_cache.get(stateful_object, new_state).all()):
                locks.append(StateLock(
                    job = job,
                    locked_item = d.stateful_object,
                    write = False
                ))

            # Take a write lock on get_stateful_object if this is a StateChangeJob
            locks.append(StateLock(
                job = job,
                locked_item = stateful_object,
                begin_state = job.old_state,
                end_state = new_state,
                write = True))

            locks.extend(job.create_locks())

        return locks

    def add_jobs(self, jobs, command):
        """Add a job, and any others which are required in order to reach its prerequisite state"""
        # Important: the Job must not be committed until all
        # its dependencies and locks are in.
        assert transaction.is_managed()

        for job in jobs:
            for dependency in self._dep_cache.get(job).all():
                if not dependency.satisfied():
                    job_log.info("add_jobs: setting required dependency %s %s" % (dependency.stateful_object, dependency.preferred_state))
                    self.set_state(dependency.get_stateful_object(), dependency.preferred_state, command)
            job_log.info("add_jobs: done checking dependencies")
            locks = self._create_locks(job)
            job.locks_json = json.dumps([l.to_dict() for l in locks])
            for l in locks:
                LockCache.add(l)
            self._create_dependencies(job)
            job.save()
            job_log.info("add_jobs: created Job %s (%s)" % (job.pk, job.description()))
            command.jobs.add(job)

    def get_transition_consequences(self, instance, new_state):
        """For use in the UI, for warning the user when an
           action is going to have some consequences which
           affect an object other than the one they are operating
           on directly.  Because this is UI rather than business
           logic, we take some shortcuts here:
            * Don't calculate expected_states, i.e. ignore running
              jobs and generate output based on the actual committed
              states of objects
            * Don't bother sorting for execution order - output an
              unordered list.
        """
        from chroma_core.models import StatefulObject
        assert(isinstance(instance, StatefulObject))

        self.expected_states = {}
        self.deps = set()
        self.edges = set()
        self._emit_transition_deps(Transition(
            instance,
            self.get_expected_state(instance),
            new_state))

        #job_log.debug("Transition %s %s->%s:" % (instance, self.get_expected_state(instance), new_state))
        #for d in self.deps:
        #    job_log.debug("  dep %s" % (d,))
        #for e in self.edges:
        #    job_log.debug("  edge [%s]->[%s]" % (e))
        self.deps = self._sort_graph(self.deps, self.edges)

        depended_jobs = []
        transition_job = None
        for d in self.deps:
            job = d.to_job()
            if isinstance(job, StateChangeJob):
                so = getattr(job, job.stateful_object)
                stateful_object_id = so.pk
                stateful_object_content_type_id = ContentType.objects.get_for_model(so).pk
            else:
                stateful_object_id = None
                stateful_object_content_type_id = None

            description = {
                'class': job.__class__.__name__,
                'requires_confirmation': job.get_requires_confirmation(),
                'confirmation_prompt': job.get_confirmation_string(),
                'description': job.description(),
                'stateful_object_id': stateful_object_id,
                'stateful_object_content_type_id': stateful_object_content_type_id
            }

            if d == self.deps[-1]:
                transition_job = description
            else:
                depended_jobs.append(description)

        return {'transition_job': transition_job, 'dependency_jobs': depended_jobs}

    def _create_dependencies(self, job):
        """Examine overlaps between self's statelocks and those of
           earlier jobs which are still pending, and generate wait_for
           dependencies when we have a write lock and they have a read lock
           or generate depend_on dependencies when we have a read or write lock and
           they have a write lock"""
        wait_fors = set()
        for lock in LockCache.get_by_job(job):
            job_log.debug("Job %s: %s" % (job, lock))
            if lock.write:
                wl = lock
                # Depend on the most recent pending write to this stateful object,
                # trust that it will have depended on any before that.
                prior_write_lock = LockCache.get_latest_write(wl.locked_item, not_job = job)
                if prior_write_lock:
                    if wl.begin_state and prior_write_lock.end_state:
                        assert (wl.begin_state == prior_write_lock.end_state), ("%s locks %s in state %s but previous %s leaves it in state %s" % (job, wl.locked_item, wl.begin_state, prior_write_lock.job, prior_write_lock.end_state))
                    job_log.debug("Job %s:   pwl %s" % (job, prior_write_lock))
                    wait_fors.add(prior_write_lock.job.id)
                    # We will only wait_for read locks after this write lock, as it
                    # will have wait_for'd any before it.
                    read_barrier_id = prior_write_lock.job.id
                else:
                    read_barrier_id = 0

                # Wait for any reads of the stateful object between the last write and
                # our position.
                prior_read_locks = LockCache.get_read_locks(wl.locked_item, after = read_barrier_id, not_job = job)
                for i in prior_read_locks:
                    job_log.debug("Job %s:   prl %s" % (job, i))
                    wait_fors.add(i.job.id)
            else:
                rl = lock
                prior_write_lock = LockCache.get_latest_write(rl.locked_item, not_job = job)
                if prior_write_lock:
                    # See comment by locked_state in StateReadLock
                    wait_fors.add(prior_write_lock.job.id)
                job_log.debug("Job %s:   pwl2 %s" % (job, prior_write_lock))

        wait_fors = list(wait_fors)
        if wait_fors:
            job.wait_for_json = json.dumps(wait_fors)

    def _sort_graph(self, objects, edges):
        """Sort items in a graph by their longest path from a leaf.  Items
           at the start of the result are the leaves.  Roots come last."""
        object_edges = defaultdict(list)
        for e in edges:
            parent, child = e
            object_edges[parent].append(child)

        leaf_distance_cache = {}

        def leaf_distance(obj, depth = 0, hops = 0):
            if obj in leaf_distance_cache:
                return leaf_distance_cache[obj] + hops

            depth = depth + 1
            max_child_hops = hops
            for child in object_edges[obj]:
                child_hops = leaf_distance(child, depth, hops + 1)
                max_child_hops = max(child_hops, max_child_hops)

            leaf_distance_cache[obj] = max_child_hops - hops

            return max_child_hops

        object_leaf_distances = []
        for o in objects:
            object_leaf_distances.append((o, leaf_distance(o)))

        object_leaf_distances.sort(lambda x, y: cmp(x[1], y[1]))
        return [obj for obj, ld in object_leaf_distances]

    def set_state(self, instance, new_state, command):
        """Return a Job or None if the object is already in new_state.
        command_id should refer to a command instance or be None."""

        job_log.info("set_state: %s-%s to state %s" % (instance.__class__, instance.id, new_state))

        from chroma_core.models import StatefulObject
        assert(isinstance(instance, StatefulObject))
        if new_state not in instance.states:
            raise SchedulingError("State '%s' is invalid for %s, must be one of %s" % (new_state, instance.__class__, instance.states))

        # Work out the eventual states (and which writelock'ing job to depend on to
        # ensure that state) from all non-'complete' jobs in the queue
        item_to_lock = LockCache.get_write_by_locked_item()
        self.expected_states = dict([(k, v.end_state) for k, v in item_to_lock.items()])

        if new_state == self.get_expected_state(instance):
            if instance.state != new_state:
                # This is a no-op because of an in-progress Job:
                job = LockCache.get_latest_write(instance).job
                command.jobs.add(job)

            command.check_completion()

            # Pick out whichever job made it so, and attach that to the Command
            return None

        self.deps = set()
        self.edges = set()
        self._emit_transition_deps(Transition(
            instance,
            self.get_expected_state(instance),
            new_state))

        # XXX
        # VERY IMPORTANT: this sort is what gives us the following rule:
        #  The order of the rows in the Job table corresponds to the order in which
        #  the jobs would run (including accounting for dependencies) in the absence
        #  of parallelism.
        # XXX
        self.deps = self._sort_graph(self.deps, self.edges)

        #job_log.debug("Transition %s %s->%s:" % (instance, self.get_expected_state(instance), new_state))
        #for e in self.edges:
        #    job_log.debug("  edge [%s]->[%s]" % (e))

        # Important: the Job must not land in the database until all
        # its dependencies and locks are in.
        for d in self.deps:
            job_log.debug("  dep %s" % d)
            job = d.to_job()
            locks = self._create_locks(job)
            job.locks_json = json.dumps([l.to_dict() for l in locks])
            for l in locks:
                LockCache.add(l)
            self._create_dependencies(job)
            job.save()
            job_log.debug("  dep %s -> Job %s" % (d, job.pk))
            command.jobs.add(job)

        command.save()

    def _emit_transition_deps(self, transition, transition_stack = {}):
        if transition in self.deps:
            job_log.debug("emit_transition_deps: %s already scheduled" % (transition))
            return transition
        else:
            job_log.debug("emit_transition_deps: %s" % (transition))
            pass

        # Update our worldview to record that any subsequent dependencies may
        # assume that we are in our new state
        transition_stack = dict(transition_stack.items())
        transition_stack[transition.stateful_object] = transition.new_state
        job_log.debug("Updating transition_stack[%s/%s] = %s" % (transition.stateful_object.__class__, transition.stateful_object.id, transition.new_state))

        # E.g. for 'unformatted'->'registered' for a ManagedTarget we
        # would get ['unformatted', 'formatted', 'registered']
        route = transition.stateful_object.get_route(transition.old_state, transition.new_state)
        job_log.debug("emit_transition_deps: route %s" % (route,))

        # Add to self.deps and self.edges for each step in the route
        prev = None
        for i in range(0, len(route) - 1):
            dep_transition = Transition(transition.stateful_object, route[i], route[i + 1])
            self.deps.add(dep_transition)
            self._collect_dependencies(dep_transition, transition_stack)
            if prev:
                self.edges.add((dep_transition, prev))
            prev = dep_transition

        return prev

    def _collect_dependencies(self, root_transition, transition_stack):
        if not hasattr(self, 'cdc'):
            self.cdc = defaultdict(list)
        if root_transition in self.cdc:
            return

        job_log.debug("collect_dependencies: %s" % root_transition)
        # What is explicitly required for this state transition?
        transition_deps = self._dep_cache.get(root_transition.to_job())
        for dependency in transition_deps.all():
            from chroma_core.lib.job import DependOn
            assert(isinstance(dependency, DependOn))
            old_state = self.get_expected_state(dependency.stateful_object)
            job_log.debug("cd %s/%s %s %s" % (dependency.stateful_object.__class__, dependency.stateful_object.id, old_state, dependency.acceptable_states))

            if not old_state in dependency.acceptable_states:
                dep_transition = self._emit_transition_deps(Transition(
                        dependency.stateful_object,
                        old_state,
                        dependency.preferred_state), transition_stack)
                self.edges.add((root_transition, dep_transition))

        def get_mid_transition_expected_state(object):
            try:
                return transition_stack[object]
            except KeyError:
                return self.get_expected_state(object)

        # What will statically be required in our new state?
        stateful_deps = self._dep_cache.get(root_transition.stateful_object, root_transition.new_state)
        for dependency in stateful_deps.all():
            if dependency.stateful_object in transition_stack:
                continue
            # When we start running it will be in old_state
            old_state = get_mid_transition_expected_state(dependency.stateful_object)

            # Is old_state not what we want?
            if old_state and not old_state in dependency.acceptable_states:
                job_log.debug("new state static requires = %s %s %s" % (dependency.stateful_object, old_state, dependency.acceptable_states))
                # Emit some transitions to get depended_on into depended_state
                dep_transition = self._emit_transition_deps(Transition(
                        dependency.stateful_object,
                        old_state,
                        dependency.preferred_state), transition_stack)
                # Record that root_dep depends on depended_on making it into depended_state
                self.edges.add((root_transition, dep_transition))

        # What was depending on our old state?
        # Iterate over all objects which *might* depend on this one
        for dependent in root_transition.stateful_object.get_dependent_objects():
            if dependent in transition_stack:
                continue
            # What state do we expect the dependent to be in?
            dependent_state = get_mid_transition_expected_state(dependent)
            for dependency in self._dep_cache.get(dependent, dependent_state).all():
                if dependency.stateful_object == root_transition.stateful_object \
                        and not root_transition.new_state in dependency.acceptable_states:
                    assert dependency.fix_state != None, "A reverse dependency must provide a fix_state: %s in state %s depends on %s in state %s" % (dependent, dependent_state, root_transition.stateful_object, dependency.acceptable_states)
                    job_log.debug("Reverse dependency: %s-%s in state %s required %s to be in state %s (but will be %s), fixing by setting it to state %s" % (
                        dependent, dependent_state, root_transition.stateful_object.__class__,
                        root_transition.stateful_object.id, dependency.acceptable_states, root_transition.new_state,
                        dependency.fix_state))

                    if hasattr(dependency.fix_state, '__call__'):
                        fix_state = dependency.fix_state(root_transition.new_state)
                    else:
                        fix_state = dependency.fix_state

                    dep_transition = self._emit_transition_deps(Transition(
                            dependent,
                            dependent_state, fix_state), transition_stack)
                    self.edges.add((root_transition, dep_transition))

    def command_run_jobs(self, job_dicts, message):
        assert(len(job_dicts) > 0)

        jobs = []
        for job in job_dicts:
            job_klass = ContentType.objects.get_by_natural_key('chroma_core', job['class_name'].lower()).model_class()
            job_instance = job_klass(**job['args'])
            jobs.append(job_instance)

        command = Command.objects.create(message = message)
        job_log.debug("command_run_jobs: command %s" % command.id)
        for job in jobs:
            job_log.debug("command_run_jobs:  job %s" % job.id)
        self.add_jobs(jobs, command)

        return command.id

    def command_set_state(self, object_ids, message):
        command = Command.objects.create(message = message)
        for ct_nk, o_pk, state in object_ids:
            model_klass = ContentType.objects.get_by_natural_key(*ct_nk).model_class()
            instance = model_klass.objects.get(pk = o_pk)
            self.set_state(instance, state, command)

        job_log.info("Created command %s (%s) with %s jobs" % (command.id, command.message, command.jobs.count()))

        return command.id
