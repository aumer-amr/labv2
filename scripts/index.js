require('dotenv').config();
const ping = require('ping');
const proxmox = require('proxmox-for-node').default;
const k8s = require('@kubernetes/client-node');
const { spawnSync } = require('child_process');

const kc = new k8s.KubeConfig();
kc.loadFromFile("../kubeconfig");

const k8sApi = kc.makeApiClient(k8s.CoreV1Api);
const k8sCustomApi = kc.makeApiClient(k8s.CustomObjectsApi);

(async () => {
  const selectedHost = await selectHost(process.env.PROXMOX_HOSTS.split(','));
  const api = proxmox({
    endpoint: {
      protocol: 'https:',
      hostname: selectedHost,
      port: 8006,
      pathname: '/'
    },
    api: process.env.PROXMOX_API
  });

  await rebootNotReadyNodes();

  await waitForNodesToBeReady();

  await resetPostgresPassword();

  await waitPodsToBeReady();

  async function waitPodsToBeReady() {
    return new Promise(async (resolve, reject) => {
      let tries = 0;
      let notReadyPods = await getNotReadyPods();
      while (notReadyPods.length > 0 || tries > 48) {
        tries++;
        console.log(`Waiting for pods ${notReadyPods} to be ready...`);
        await new Promise((resolve) => setTimeout(resolve, 10000));
        notReadyPods = await getNotReadyPods();
      }

      if (tries > 48) {
        return reject(new Error(`Pods ${notReadyPods} did not become ready in time.`));
      }

      console.log('All pods ready');
      return resolve();
    });
  }

  async function getNotReadyPods() {
    const notReadyPods = [];
    const pods = await k8sApi.listPodForAllNamespaces();
    pods.body.items.forEach((pod) => {
      if (pod.status.conditions.filter((condition) => condition.type === 'Ready' && condition.status === 'False' && condition.reason !== "PodCompleted").length > 0) {
        console.log(`Pod ${pod.metadata.name} is not ready.`);
        notReadyPods.push(pod.metadata.name);
      }
    });
    return notReadyPods;
  }

  async function resetPostgresPassword() {
    return new Promise(async (resolve, reject) => {
      const cluster = await k8sCustomApi.getNamespacedCustomObject('postgresql.cnpg.io', 'v1', 'database', 'clusters', 'postgres');
      const primary = cluster.body.status.targetPrimary;

      console.log(`Found primary database node: ${primary}`);

      const psqlCommand = `psql -c "ALTER USER postgres WITH PASSWORD '${process.env.POSTGRES_PASS}';"`;
      const commandArgs = ['exec', primary, '-n', 'database', '--', 'bash', '-c', psqlCommand];

      const result = spawnSync('kubectl', commandArgs, { encoding: 'utf-8' });

      // Handle the result
      if (result.error) {
        console.error(`Execution error: ${result.error.message}`);
        return reject();
      }

      if (result.stdout.includes('ALTER ROLE')) {
        console.log('Password reset successfully.');
        return resolve();
      }

      if (result.status !== 0) {
        console.error('Command failed.');
        return reject();
      }
    });
  }

  async function waitForNodesToBeReady() {
    return new Promise(async (resolve, reject) => {
      let tries = 0;
      let notReadyNodes = await getK8sNotReadyNodes();
      while (notReadyNodes.length > 0 || tries > 12) {
        tries++;
        console.log(`Waiting for nodes ${notReadyNodes} to be ready...`);
        await new Promise((resolve) => setTimeout(resolve, 10000));
        notReadyNodes = await getK8sNotReadyNodes();
      }

      if (tries > 12) {
        return reject(new Error(`Nodes ${notReadyNodes} did not become ready in time.`));
      }

      console.log('All nodes ready');
      return resolve();
    });
  }

  async function getK8sNotReadyNodes() {
    const notReadyNodes = [];
    const nodes = await k8sApi.listNode();
    nodes.body.items.forEach((node) => {
      if (node.status.conditions.filter((condition) => condition.type === 'Ready' && condition.status === 'False').length > 0) {
        console.log(`Node ${node.metadata.name} is not ready.`);
        notReadyNodes.push(node.metadata.name);
      }
    });
    return notReadyNodes;
  }

  async function rebootNotReadyNodes() {
    const notReadNodes = await getK8sNotReadyNodes();
    const vmIds = await collectVmIds();
    for await (const node of notReadNodes) {
      const vm = vmIds[`${node}-k8s`];
      if (vm) {
        console.log(`Rebooting VM ${node} (ID: ${vm.id})...`);
        await rebootVm(vm);
      } else {
        console.error(`VM ID not found for node ${node}.`);
      }
    }
  }

  async function rebootVm(vm) {
    return new Promise((resolve, reject) => {
      api.qemu.stop(vm.node, vm.id, (err, response) => {
        if (err) {
          return reject(err);
        }
        console.log('VM stopped:', response);
        setTimeout(() => {
          api.qemu.start(vm.node, vm.id, (err, response) => {
            if (err) {
              return reject(err);
            }
            console.log('VM started:', response);
            return resolve();
          });
        }, 10000);
      });
    });
  }

  async function collectVmIds() {
    const vmIds = {};
    const proxmoxNodes = await getProxmoxNodes();
    console.log('Proxmox nodes:', proxmoxNodes);
    for await (const node of proxmoxNodes) {
      const vmList = await getProxmoxNodeVms(node);
      for (const vm of vmList) {
        vmIds[vm.name] = {
          id: vm.vmid,
          node: node
        };
      }
    }
    return vmIds;
  }

  function getProxmoxNodeVms(node) {
    return new Promise((resolve, reject) => {
      api.getQemu(node, (err, response) => {
        if (err) {
          return reject(err);
        }
        const data = JSON.parse(response).data;
        const nodeVms = data.filter(vm => vm.name.includes('k8s')).map((vm) => {
          return {
            name: vm.name,
            vmid: vm.vmid
          };
        });
        return resolve(nodeVms);
      });
    });
  }

  function getProxmoxNodes() {
    return new Promise((resolve, reject) => {
      api.getClusterStatus((err, response) => {
        if (err) {
          return reject(err);
        }
        const data = JSON.parse(response).data;
        if (data.filter((node) => node.type === 'node' && node.online === 0).length > 0) {
          return reject(new Error('Not all nodes are online.'));
        }
        return resolve(data.filter((node) => node.type === 'node' && node.online === 1).map((node) => node.name));
      });
    });
  }

  function selectHost(hosts) {
    return new Promise((resolve, reject) => {
      function pingNextHost(index) {
        if (index >= hosts.length) {
          return reject(new Error('All hosts failed to respond.'));
        }

        const host = hosts[index];
        console.log(`Pinging ${host}...`);

        ping.promise.probe(host)
          .then(result => {
            if (result.alive) {
              console.log(`Selected host: ${host}`);
              return resolve(host);
            } else {
              return pingNextHost(index + 1);
            }
          })
          .catch(error => {
            console.error(`Error pinging ${host}: ${error}`);
            return pingNextHost(index + 1);
          });
      }

      pingNextHost(0);
    });
  }
})();
