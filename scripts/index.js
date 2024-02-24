require('dotenv').config();
const ping = require('ping');
const proxmox = require('proxmox-for-node').default;

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

  rebootNotReadyNodes();

  async function rebootNotReadyNodes() {
    const notReadNodes = await kubectlGetNotReadyNodes();
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

  function kubectlGetNotReadyNodes() {
    process.chdir('../');
    return new Promise((resolve, reject) => {
      const { exec } = require('child_process');
      exec('kubectl get nodes | grep NotReady | awk \'{ print $1 }\'', (error, stdout, stderr) => {
        if (error) {
          return reject(error);
        }
        return resolve(stdout.split('\n').filter(Boolean));
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
