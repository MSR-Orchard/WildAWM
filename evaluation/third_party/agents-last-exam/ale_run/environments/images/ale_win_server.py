"""``ale-win-server`` — Windows Server 2022 baked image with an NVIDIA L4 GPU.

GPU counterpart to :mod:`.ale_win10`: same field set, same baked layout
(node + cua-mcp-server + ``C:\\ale-run`` harness venv + ``E:\\agenthle``
task data on the boot disk), but provisioned on ``g2`` machine types which
carry an integrated L4. Absolute paths reflect this image's profile dir,
which is lowercase ``C:\\Users\\user`` (vs ``C:\\Users\\User`` on ale-win10)."""
from __future__ import annotations

from . import Image


IMAGE = Image(
    name="ale-win-server",
    os="windows",

    # sandbox-side paths (Server profile dir is lowercase ``user``)
    work_dir_base=r"C:\Users\user\.ale",
    task_data_root=r"E:\agenthle",
    node=r"C:\Users\user\node-v24.12.0-win-x64\node.exe",
    # Image-baked dedicated venv (Python 3.12 + pydantic + requests + PyYAML).
    # Same role as ale-win10's ``C:\ale-run\.venv``.
    python=r"C:\ale-run\.venv\Scripts\python.exe",
    mcp_server_dir=r"C:\Users\user\cua_mcp_server",

    # provisioning defaults — g2 carries an integrated NVIDIA L4
    default_machine_type="g2-standard-8",
    gpu="nvidia-l4",

    # cua-server port on GCE-backed images
    cua_server_port=5000,
)
