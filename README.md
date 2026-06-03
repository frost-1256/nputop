# npu-top

`npu-top` is a top-like terminal monitor for Intel AI Boost NPUs on Linux/NixOS.
It reads the upstream `intel_vpu` sysfs counters and does not need root for the
normal view.

## What It Shows

- NPU utilization calculated from `npu_busy_time_us` deltas.
- Current and maximum NPU frequency when exposed by the kernel.
- Resident NPU memory from `npu_memory_utilization`.
- Runtime power state, scheduler mode, PCI ID, and driver version.
- Curses UI, line output, JSON output, and device listing.

## Run

```sh
nix run .#npu-top
nix run .#npu-top -- --once
nix run .#npu-top -- --json --once
nix run .#npu-top -- --list
```

The command also installs an alias named `intel-ai-boost-top`.

From GitHub:

```sh
nix run github:frost-1256/npu-top
nix run github:frost-1256/npu-top -- --once
```

If the directory is not a valid Git checkout, force Nix to treat it as a plain
path:

```sh
nix run path:$PWD#npu-top
```

## NixOS Module

Add the flake as an input and enable the module:

```nix
{
  inputs.npu-top.url = "path:/home/spring/npu-llm/npu-top";

  outputs = { self, nixpkgs, npu-top, ... }: {
    nixosConfigurations.host = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        npu-top.nixosModules.default
        {
          programs.npu-top.enable = true;
        }
      ];
    };
  };
}
```

This installs `npu-top`, requests the `intel_vpu` kernel module, and enables
redistributable firmware by default.

## Requirements

- Intel Core Ultra or newer with Intel AI Boost / NPU.
- Linux kernel with the `intel_vpu` accelerator driver.
- NPU firmware available to the kernel.
- A readable `/sys/class/accel/accel*` or
  `/sys/bus/pci/drivers/intel_vpu/*/npu_busy_time_us`.

The kernel documentation recommends reading `npu_busy_time_us` about once per
second, so the default refresh interval is `1.0s`.
