#! /usr/bin/env python
# -*- coding: utf-8 -*-

import base64
import json
import math
import nixops.resources
import nixops.statefile
import os
import subprocess
import sys
import tempfile
import time

own_modules = os.path.realpath(os.path.dirname(__file__) + "/../share/nix/hydra-provisioner")
if not os.path.exists(own_modules):
    own_modules = os.path.dirname(__file__)

def log(s):
    sys.stderr.write(s + "\n")

def get_new_deployment_name(prefix, depls):
    """Generate a unique NixOps deployment name with the given prefix."""
    names = {depl.name for depl in depls}
    i = 0
    while True:
        name = prefix + "-" + str(i)
        if name not in names: break
        i += 1
    return name

def get_depl_arg(depl, key, default=""):
    s = depl.args.get(key, default)
    return s.replace('"', '') # FIXME: escaping

def get_depl_time_left(depl):
    m = depl.machines.get("machine", None)
    if not m: return 0
    next_charge_time = m.next_charge_time()
    if not next_charge_time: return 0
    return max(next_charge_time - int(time.time()), 0)

def depl_state(depl):
    machine = depl.machines.get("machine", None)
    return machine.state if machine else nixops.resources.ResourceState.MISSING

def main() -> None:
    # Read the config file.
    if len(sys.argv) != 2:
        sys.stderr.write("Syntax: hydra-provisioner <CONFIG-FILE>\n")
        sys.exit(1)
    config_file = sys.argv[1]

    config = json.loads(subprocess.check_output(
        ["nix-instantiate", "--eval", "--strict", "--json", config_file]))

    if "systemTypes" not in config: config["systemTypes"] = {}

    tag = config.get("tag", "hydra-provisioned")

    # Get the current deployments.
    sf = nixops.statefile.StateFile(nixops.statefile.get_default_state_file())
    all_depls = sf.get_all_deployments()
    depls = [depl for depl in all_depls if get_depl_arg(depl, "tag") == tag]

    # Get status info from the Hydra queue runner.
    # FIXME: handle error.
    status_command = config.get("statusCommand", ["hydra-queue-runner", "--status"])
    try:
        status = json.loads(subprocess.check_output(status_command))
    except subprocess.CalledProcessError:
        status = None

    if not status or status["status"] == "down":
        status = {"status": "down", "machineTypes": {}, "machines": {}, "uptime": 0}

    # Squash i686-linux into x86_64-linux. We assume there are no actual
    # i686-linux build machines.
    for type_name in status["machineTypes"].keys():
        if type_name.startswith("i686-linux"):
            target_name = type_name.replace("i686-linux", "x86_64-linux")
            type_status = status["machineTypes"][type_name]
            if target_name in status["machineTypes"]:
                status["machineTypes"][target_name]["runnable"] += type_status["runnable"]
            else:
                status["machineTypes"][target_name] = type_status
            del status["machineTypes"][type_name]

    system_types = set(status["machineTypes"].keys()).union(set(config["systemTypes"].keys()))

    # For each machine type, determine how many machines are needed, and
    # create new machines if necessary.
    in_use = set({})
    up_to_date = set({})

    for type_name in system_types:
        type_status = status["machineTypes"].get(type_name, {"runnable": 0})
        type_config = config["systemTypes"].get(type_name, None)
        if not type_config:
            log("cannot provision machines of type {0}".format(type_name))
            continue

        runnable = type_status["runnable"]
        ignored_runnables = type_config.get("ignoredRunnables", 0)
        runnables_per_machine = type_config.get("runnablesPerMachine", 10)
        wanted = int(math.ceil(max(runnable - ignored_runnables, 0) / float(runnables_per_machine)))
        allowed = min(max(wanted, type_config.get("minMachines", 0)), type_config.get("maxMachines", 1))
        log("machine type {0} has {1} runnables, wants {2} machines, will get {3} machines"
            .format(type_name, runnable, wanted, allowed))

        def depl_sort_key(depl):
            x = [depl_state(depl) != nixops.resources.ResourceState.UP]
            return x

        existing = [depl for depl in depls if get_depl_arg(depl, "type") == type_name]
        existing.sort(key=depl_sort_key)

        # FIXME: error handling.
        have = 0
        created = 0
        while have < allowed:
            check = False

            if len(existing) == 0:
                # Create a new machine.
                # FIXME: make this transactional.
                name = get_new_deployment_name(tag, depls)

                depl = sf.create_deployment()
                depl.name = name
                depl.set_argstr("type", type_name)
                depl.set_argstr("tag", tag)
                depls.append(depl)
                all_depls.append(depl)

                log("created deployment ‘{0}’ of type ‘{1}’".format(name, type_name))
                created += 1

            else:
                depl = existing[0]

                if depl_state(depl) == nixops.resources.ResourceState.UP:
                    # We have an existing machine and it's up. Check
                    # whether it's really up, and if so, use it.

                    depl.machines["machine"].check() # FIXME: only do this periodically
                    if depl_state(depl) != nixops.resources.ResourceState.UP:
                        # It's not actually up. Resort and retry.
                        existing.sort(key=depl_sort_key)
                        continue

                    #up_to_date.add(depl) # FIXME

                elif depl_state(depl) == nixops.resources.ResourceState.MISSING:
                    existing.pop(0)
                    continue

                existing.pop(0)

            depl.nix_exprs = [os.path.abspath(type_config["nixopsExpr"])]
            depl.nix_path = [nixops.util.abs_nix_path(x) for x in type_config.get("nixPath", [])]

            in_use.add(depl)

            have += 1

            if created >= 1: break

    # Keep recently used machines in nix.machines.
    expired = set({})
    unusable = set({})
    for depl in depls:
        if depl in in_use: continue

        if depl_state(depl) not in [nixops.resources.ResourceState.UP, nixops.resources.ResourceState.STARTING]:
            expired.add(depl)
            continue

        type_name = get_depl_arg(depl, "type")
        type_config = config["systemTypes"].get(type_name, None)
        type_status = status["machineTypes"].get(type_name, None)

        grace_period = type_config.get("gracePeriod", 0) if type_config else 0

        # Keep machines that still have at least 30 minutes of paid time
        # left.
        time_left = get_depl_time_left(depl)
        if time_left >= 30 * 60:
            log("keeping deployment ‘{0}’ because it has {1}s left".format(depl.name, time_left))
            in_use.add(depl)
            continue

        # Keep machines that are currently in use. FIXME: we may want to
        # destroy them anyway, in order not to keep an excessive number of
        # machines around. Hydra will retry aborted build steps anyway.
        m = depl.machines.get("machine", None)
        machine_status = status["machines"].get("root@" + m.get_ssh_name(), {})
        if machine_status and machine_status.get("currentJobs", 0) != 0:
            log("keeping active deployment ‘{0}’".format(depl.name))
            in_use.add(depl)

            # If this machine doesn't have a grace period, then don't add
            # it to the machines list. This prevents new builds from
            # starting.
            if grace_period > 0:
                unusable.add(depl)

            continue

        # Keep machines that have been used within the last ‘gracePeriod’
        # seconds.
        last_active = type_status.get("lastActive", 0) if type_status else 0
        if last_active == 0: last_active = int(time.time()) - status["uptime"] + 1800

        if int(time.time()) - last_active < grace_period:
            log("keeping recently used deployment ‘{0}’".format(depl.name))
            in_use.add(depl)
            continue

        expired.add(depl)

    # Deploy the active machines. FIXME: do in parallel.
    deployed = set({})
    for depl in in_use:
        if depl not in up_to_date:
            log("updating deployment ‘{0}’...".format(depl.name))
            depl.extra_nix_path.append("hydra-provisioner=" + own_modules)
            try:
                depl.deploy(check=True)
                depl.machines["machine"].ssh.run_command(["touch", "/run/keep-alive"])
                deployed.add(depl)
            except Exception as e:
                log("error deploying ‘{0}’: {1}".format(depl.name, e))
                continue
        deployed.add(depl)

    # Generate the new nix.machines.
    machines_list = []
    for depl in deployed:
        if depl in unusable: continue

        m = depl.machines.get("machine", None)
        assert(m)

        type_name = get_depl_arg(depl, "type")
        type_config = config["systemTypes"][type_name]

        if ":" not in type_name: type_name += ":"
        (systems, features) = type_name.split(":", 1)
        systems_list = systems.split(",")
        features_list = features.split(",") if features != "" else []
        if "x86_64-linux" in systems_list and "i686-linux" not in systems_list:
            systems_list.append("i686-linux")

        columns = [
            "root@" + m.get_ssh_name(),
            ",".join(systems_list),
            type_config.get("sshKey", "-"),
            str(type_config.get("maxJobs", 1)),
            str(type_config.get("speedFactor", 1)),
            ",".join(features_list) if features_list else "-",
            ",".join(features_list) if features_list else "-",
            base64.b64encode(m.public_host_key) if m.public_host_key else "-"
        ]

        assert(all(c != "" for c in columns))

        machines_list.append(" ".join(columns) + "\n")

    machines_file = "".join(machines_list)
    update_command = config.get("updateCommand", None)
    if update_command:
        machines_tmp = tempfile.NamedTemporaryFile()
        machines_tmp.write(machines_file)
        machines_tmp.seek(0)
        subprocess.check_call(update_command, stdin=machines_tmp)
    else:
        nixops.util.write_file("/var/lib/hydra/provisioner/machines", machines_file)

    # Stop or destroyed unused machines.
    for depl in expired:
        type_name = get_depl_arg(depl, "type")
        type_config = config["systemTypes"].get(type_name, None)

        if depl_state(depl) in [nixops.resources.ResourceState.UP, nixops.resources.ResourceState.STARTING]:

            # Don't stop/destroy machines that still have at least 10 minutes
            # of paid time left.
            time_left = get_depl_time_left(depl)
            if time_left >= 10 * 60:
                log("not stopping/destroying deployment ‘{0}’ because it has {1}s left".format(depl.name, time_left))
                continue

        stop_on_idle = type_config.get("stopOnIdle", False) if type_config else False

        if stop_on_idle:
            if depl_state(depl) != nixops.resources.ResourceState.STOPPED:
                log("stopping deployment ‘{0}’".format(depl.name))
                depl.stop_machines()

        else:
            log("destroying deployment ‘{0}’".format(depl.name))
            depl.logger.set_autoresponse("y")
            depl.destroy_resources()
            depl.delete()

if __name__ == "__main__":
    main()
