"""Google Cloud setup instructions and the no-warranty disclaimer, shown
before first use.

Printed by install.py after installing the CLI, and by the wrapper itself
when gcloud isn't found on PATH (gcp.GcloudNotFoundError) — the two moments
a user is most likely to need them.

Author: Yves R. Shema <yshema@bcit.ca>
Co-Authored-By: Claude <noreply@anthropic.com>
"""

from __future__ import annotations

import sys

GCLOUD_INSTALL_URL = "https://cloud.google.com/sdk/docs/install"
GCP_FREE_TRIAL_URL = "https://cloud.google.com/free"

SETUP_INSTRUCTIONS = f"""\
Before using this tool, you need a Google Cloud project with billing enabled:

1. Create a Google Cloud account and redeem the $300 / 90-day free trial:
   {GCP_FREE_TRIAL_URL}
   Eligibility requires never having had a Google Cloud trial before. The
   90 days runs from signup, not from first use.
2. Create a Google Cloud project (or use one already set up for you), and
   confirm billing is enabled on it.
3. Install the Google Cloud CLI:
   {GCLOUD_INSTALL_URL}
4. Create a named gcloud configuration and log in:
     gcloud config configurations create bcit-2620
     gcloud auth login
     gcloud config set project <YOUR_PROJECT_ID>
   A configuration bundles account + project + zone under one name, so this
   won't interfere with any other Google Cloud project on this machine.
"""

DISCLAIMER = """\
This software is provided "as is", without warranty of any kind, express
or implied, including but not limited to the warranties of
merchantability, fitness for a particular purpose, and noninfringement. In
no event shall the authors be liable for any claim, damages, or other
liability arising from, out of, or in connection with the software or the
use or other dealings in the software.
"""


def print_setup_instructions(out=None) -> None:
    print(SETUP_INSTRUCTIONS, file=out or sys.stdout)


def print_disclaimer(out=None) -> None:
    print(DISCLAIMER, file=out or sys.stdout)
