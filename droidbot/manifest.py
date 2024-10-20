from lxml import etree

class AppManifest:
    def __init__(self, manifest_path: str):
        self.manifest_path = manifest_path
        with open(manifest_path, 'rb') as f:
            self.tree: etree.Element = etree.fromstring(f.read().decode('utf-8', 'replace'))