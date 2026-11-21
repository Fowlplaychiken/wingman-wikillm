"""Verify the complete published GNU FDL text, allowing printed or HTML list labels."""
import hashlib
import re


# Normalized SPDX license-list-data text/GFDL-{version}-only.txt, either with
# the printed clause letters or with those section 4 list labels supplied by HTML.
_DIGESTS = {
    "d5ce014090c7e2e6af67c484e579fd416ebc38c072d779d86fc7a4f236c0f404": "GFDL-1.1",
    "9a3a8f99db1612d882883bbb71c0690c92150e024308b9784f24b6cbcfeffc02": "GFDL-1.1",
    "1ca7cc642947084d3c9f49f5e39c3565f04afe519bc04e56168f15feaed479c1": "GFDL-1.2",
    "6fcd901a8a424940241d06eb4dab0cc44e2e7a1bff39170d20aa9411094fe036": "GFDL-1.2",
    "bef2b6a554be8d3ed7bd41e8a070b6b5c5704f3c41beed5ef771e0cc885bf774": "GFDL-1.3",
    "37686de09ca0f8243b7d65128f9114fb6d330ca30bd3b5bebadf273064623494": "GFDL-1.3",
}


def gfdl_license(blocks):
    words = " ".join(re.findall(r"[a-z0-9]+", " ".join(blocks).lower()))
    words = re.sub(r"^(?:license |gnu free documentation license )+", "", words)
    words = words.replace("addendum how to use this license", "how to use this license")
    return _DIGESTS.get(hashlib.sha256(words.encode("ascii")).hexdigest())
