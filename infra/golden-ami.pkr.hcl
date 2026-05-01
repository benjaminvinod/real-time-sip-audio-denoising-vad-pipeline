# golden-ami.pkr.hcl  –  Benjamin Vinod | Module 1
# Bakes a Golden AMI with Python 3.11, the denoise pipeline installed
# as a systemd service, ready for the ASG launch template.
#
# Usage
# -----
#   packer init .
#   packer build golden-ami.pkr.hcl
#
# Required env vars
#   AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY  (or an IAM instance role)
#   PKR_VAR_region   – overrides the default region below

packer {
  required_plugins {
    amazon = {
      version = ">= 1.3.0"
      source  = "github.com/hashicorp/amazon"
    }
  }
}

# ── variables ────────────────────────────────────────────────────────────────

variable "region" {
  type    = string
  default = "ap-south-1"
}

variable "instance_type" {
  type    = string
  default = "t3.medium"
}

variable "app_version" {
  type    = string
  default = "1.0.0"
}

# ── source: find the latest Ubuntu 22.04 LTS AMI ─────────────────────────────

source "amazon-ebs" "denoise_pipeline" {
  region        = var.region
  instance_type = var.instance_type

  # Canonical Ubuntu 22.04 LTS (HVM SSD)
  source_ami_filter {
    filters = {
      name                = "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    owners      = ["099720109477"]   # Canonical
    most_recent = true
  }

  ssh_username = "ubuntu"

  ami_name        = "denoise-pipeline-golden-${var.app_version}-{{timestamp}}"
  ami_description = "Golden AMI – Real-Time Denoise+VAD Pipeline v${var.app_version}"

  tags = {
    Name        = "denoise-pipeline-golden"
    Version     = var.app_version
    Environment = "production"
    BuildDate   = "{{timestamp}}"
  }

  # Encrypt the root volume at rest
  encrypt_boot = true
}

# ── build steps ──────────────────────────────────────────────────────────────

build {
  sources = ["source.amazon-ebs.denoise_pipeline"]

  # ── 1. OS baseline ──────────────────────────────────────────────────────────
  provisioner "shell" {
    inline = [
      "sudo apt-get update -y",
      "sudo apt-get upgrade -y",
      "sudo apt-get install -y python3.11 python3.11-venv python3.11-dev",
      "sudo apt-get install -y build-essential libsndfile1 ffmpeg git curl",
      # audioop is in the stdlib for Python ≤ 3.12; ensure it's available
      "python3.11 -c 'import audioop' || echo 'audioop not found – install audioop-lts'",
    ]
  }

  # ── 2. Copy application files ───────────────────────────────────────────────
  provisioner "file" {
    sources = [
      "sip_server.py",
      "denoiseVADHandler.py",
      "denoisevadserver.py",
      "metricsLogger.py",
      "appConfig.py",
      "requirements.txt",
    ]
    destination = "/tmp/"
  }

  # ── 3. Install into /opt/denoise-pipeline ──────────────────────────────────
  provisioner "shell" {
    inline = [
      "sudo mkdir -p /opt/denoise-pipeline",
      "sudo cp /tmp/*.py /opt/denoise-pipeline/",
      "sudo cp /tmp/requirements.txt /opt/denoise-pipeline/",

      # create venv and install deps
      "sudo python3.11 -m venv /opt/denoise-pipeline/venv",
      "sudo /opt/denoise-pipeline/venv/bin/pip install --upgrade pip",
      "sudo /opt/denoise-pipeline/venv/bin/pip install -r /opt/denoise-pipeline/requirements.txt",
      # audioop-lts is needed on Python 3.12+ (soft dep)
      "sudo /opt/denoise-pipeline/venv/bin/pip install audioop-lts || true",
      # boto3 for CloudWatch
      "sudo /opt/denoise-pipeline/venv/bin/pip install boto3",

      # permissions
      "sudo chown -R ubuntu:ubuntu /opt/denoise-pipeline",
    ]
  }

  # ── 4. Install systemd service ──────────────────────────────────────────────
  provisioner "file" {
    source      = "denoise-pipeline.service"
    destination = "/tmp/denoise-pipeline.service"
  }

  provisioner "shell" {
    inline = [
      "sudo cp /tmp/denoise-pipeline.service /etc/systemd/system/",
      "sudo systemctl daemon-reload",
      "sudo systemctl enable denoise-pipeline.service",
    ]
  }

  # ── 5. Harden & clean up ────────────────────────────────────────────────────
  provisioner "shell" {
    inline = [
      "sudo apt-get autoremove -y",
      "sudo apt-get clean",
      "sudo rm -rf /tmp/*",
      # remove SSH host keys so each instance gets fresh ones
      "sudo shred -u /etc/ssh/ssh_host_*",
    ]
  }
}
