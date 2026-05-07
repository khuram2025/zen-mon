variable "output_dir" {
  type    = string
  default = "sensor-appliance/out"
}

variable "iso_url" {
  type    = string
  default = "https://releases.ubuntu.com/24.04/ubuntu-24.04.1-live-server-amd64.iso"
}

variable "iso_checksum" {
  type    = string
  default = "none"
}

variable "sensor_binary" {
  type    = string
  default = "sensor-appliance/out/zenplus-sensor"
}

packer {
  required_plugins {
    qemu = {
      source  = "github.com/hashicorp/qemu"
      version = ">= 1.1.0"
    }
  }
}

source "qemu" "zenplus_sensor" {
  accelerator      = "kvm"
  iso_url          = var.iso_url
  iso_checksum     = var.iso_checksum
  output_directory = "${var.output_dir}/qemu"
  vm_name          = "zenplus-sensor"
  disk_size        = "20000M"
  memory           = 1024
  cpus             = 1
  format           = "qcow2"
  headless         = true
  shutdown_command = "echo 'packer' | sudo -S shutdown -P now"

  # This template is intentionally a scaffold. The autoinstall seed should be
  # added in the release build pipeline together with the compiled sensor
  # binary. Keep the appliance minimal and locked down.
}

build {
  sources = ["source.qemu.zenplus_sensor"]

  provisioner "file" {
    source      = "sensor-appliance"
    destination = "/tmp/zenplus-sensor"
  }

  provisioner "file" {
    source      = var.sensor_binary
    destination = "/tmp/zenplus-sensor-bin"
  }

  provisioner "shell" {
    inline = [
      "sudo mkdir -p /opt/zenplus-sensor",
      "sudo cp -R /tmp/zenplus-sensor/* /opt/zenplus-sensor/",
      "sudo chmod +x /opt/zenplus-sensor/scripts/*.sh",
      "sudo install -m 0755 /tmp/zenplus-sensor-bin /tmp/zenplus-sensor",
      "sudo ZENPLUS_SENSOR_BIN=/tmp/zenplus-sensor /opt/zenplus-sensor/scripts/install-sensor.sh",
    ]
  }

  post-processor "shell-local" {
    inline = [
      "mkdir -p ${var.output_dir}",
      "echo 'Export the built VM to ${var.output_dir}/zenplus-sensor.ova and ${var.output_dir}/zenplus-sensor.ovf using your hypervisor tooling.'",
    ]
  }
}
