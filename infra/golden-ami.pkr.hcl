packer {
  required_plugins {
    amazon = {
      source  = "github.com/hashicorp/amazon"
      version = ">= 1.0.0"
    }
  }
}

variable "aws_region" {
  default = "ap-south-1"
}

source "amazon-ebs" "ubuntu" {
  region                  = var.aws_region
  instance_type           = "t2.medium"
  source_ami_filter {
    filters = {
      name                = "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    owners      = ["099720109477"]
    most_recent = true
  }
  ssh_username            = "ubuntu"
  ami_name                = "denoise-pipeline-ami-{{timestamp}}"
}

build {
  sources = ["source.amazon-ebs.ubuntu"]

  provisioner "shell" {
    inline = [
      "sudo apt update",
      "sudo apt install -y python3 python3-pip git",

      "cd /home/ubuntu",
      "git clone https://github.com/benjaminvinod/real-time-sip-audio-denoising-vad-pipeline.git",

      "cd real-time-sip-audio-denoising-vad-pipeline",
      "pip3 install -r requirements.txt",

      "sudo cp infra/denoise-pipeline.service /etc/systemd/system/",
      "sudo systemctl daemon-reexec",
      "sudo systemctl daemon-reload",
      "sudo systemctl enable denoise-pipeline.service"
    ]
  }
}