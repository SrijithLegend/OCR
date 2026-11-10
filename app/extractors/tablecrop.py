"""Table Transformer table detection (claude.md Phase 2). Optional, off by default (TABLE_CROP=1).

Used to OCR each detected table at full resolution as extra EVIDENCE for scans, instead of coarse page
bands. Per the guide it is an ablation: keep it only if it lowers the silent-error rate on held-out files.
Needs `timm`; the detector (~100 MB) downloads on first use and runs fine on CPU.
"""
_DET = None


def detect_tables(img, threshold=0.9, margin=0.01):
    """-> list of PIL crops, one per detected table, top to bottom. [] when none found."""
    global _DET
    import torch
    from transformers import AutoImageProcessor, TableTransformerForObjectDetection
    if _DET is None:
        name = "microsoft/table-transformer-detection"
        _DET = (AutoImageProcessor.from_pretrained(name),
                TableTransformerForObjectDetection.from_pretrained(name).eval())
    proc, model = _DET
    with torch.no_grad():
        out = model(**proc(images=img, return_tensors="pt"))
    res = proc.post_process_object_detection(out, threshold=threshold,
                                             target_sizes=torch.tensor([img.size[::-1]]))[0]
    w, h = img.size
    mx, my = int(w * margin), int(h * margin)
    boxes = sorted((b.tolist() for b in res["boxes"]), key=lambda b: b[1])
    return [img.crop((max(0, int(x0) - mx), max(0, int(y0) - my), min(w, int(x1) + mx), min(h, int(y1) + my)))
            for x0, y0, x1, y1 in boxes]
