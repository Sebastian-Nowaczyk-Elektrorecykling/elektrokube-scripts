this is a repository downloaded onto a first machine in cluster that will be used to administer the cluster

it will install k3s with Cilium onto the first node with Cilium and k3s configured to let Cilium take over what it can

the hosts will be prepared to not sleep or suspend with power button rebooting the machine

it will have a script that connects to a node to be added to the cluster through ssh (ask user for credentials) and set up so the new node cannot be accessed through ssh except for the first node through a key downloaded onto the first node
then that access will be used to install k3s onto the new node (keeping network parts off for Cilium) and join the cluster

later it will also have scripts that add FluxCD repositories to the cluster without writing anything to any repository, just reconciliation
it needs a way to ask for GH token in case the repository is private but I expect most of them to be public so check first
