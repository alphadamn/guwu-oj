# build.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $MyInvocation.MyCommand.Path -Parent)  # 切换到脚本所在目录
Set-Location ..                                                # 再切换到上级目录（即项目根目录）

$APT_MIRROR = if ($env:APT_MIRROR) { $env:APT_MIRROR } else { "mirrors.tuna.tsinghua.edu.cn" }

$images = @(
    @{ context = "container_py"; tag = "oj-python:latest" },
    @{ context = "container_java"; tag = "oj-java:latest" },
    @{ context = "container_cpp"; tag = "oj-cpp:latest" },
    @{ context = "container_c"; tag = "oj-c:latest" },
    @{ context = "container_other"; tag = "oj-other:latest" }
)

foreach ($entry in $images) {
    $ctx = $entry.context
    $tag = $entry.tag
    Write-Host "=== Building $tag from docker/judge/$ctx ==="
    docker build `
        --build-arg "APT_MIRROR=${APT_MIRROR}" `
        -t "$tag" `
        "docker/judge/$ctx"
    Write-Host ""
}

Write-Host "All 5 images built:"
docker images | Select-String "^oj-"