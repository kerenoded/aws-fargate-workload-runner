resource "aws_vpc" "this" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(local.common_tags, { Name = local.name })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = merge(local.common_tags, { Name = local.name })
}

resource "aws_subnet" "public" {
  for_each = {
    for idx, az in slice(data.aws_availability_zones.available.names, 0, 2) :
    az => { az = az, cidr = cidrsubnet(aws_vpc.this.cidr_block, 8, idx) }
  }

  vpc_id            = aws_vpc.this.id
  cidr_block        = each.value.cidr
  availability_zone = each.value.az

  # true: Fargate tasks need outbound internet access to reach AWS public endpoints
  # (S3, IoT, SQS, ECR). This is the simplest approach — public subnets + IGW,
  # no NAT Gateway required. run_task.py sets assignPublicIp=ENABLED explicitly,
  # so tasks always get a public IP regardless of this flag.
  map_public_ip_on_launch = true

  tags = merge(local.common_tags, { Name = "${local.name}-public-${each.key}" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = merge(local.common_tags, { Name = "${local.name}-public" })
}

resource "aws_route_table_association" "public" {
  for_each       = aws_subnet.public
  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}
