"""Isolate ink from paper backgrounds without synthesizing handwriting."""
from io import BytesIO
from PIL import Image, ImageOps, ImageFilter, ImageChops


def clean_signature_image(source):
    with Image.open(source) as original:
        if original.width * original.height > 20_000_000:
            raise ValueError("Signature image is too large; crop the handwriting first")
        image = ImageOps.exif_transpose(original).convert("RGBA")
    image.thumbnail((1600, 1000), Image.Resampling.LANCZOS)
    red, green, blue, existing_alpha = image.split()
    # Flatten transparent pixels to white only when estimating the paper.
    paper = Image.new("RGBA", image.size, "white")
    paper.alpha_composite(image)
    rgb = paper.convert("RGB")
    gray = ImageOps.grayscale(rgb)
    local_paper = gray.filter(ImageFilter.MaxFilter(31))
    darkness = ImageChops.subtract(local_paper, gray)
    r, g, b = rgb.split()
    saturation = ImageChops.subtract(ImageChops.lighter(ImageChops.lighter(r,g),b),
                                     ImageChops.darker(ImageChops.darker(r,g),b))
    ink = ImageChops.lighter(darkness, saturation)
    alpha = ink.point(lambda value: 0 if value < 20 else min(255, (value-20)*5))
    alpha = ImageChops.multiply(alpha, existing_alpha)
    bounds = alpha.point(lambda value: 255 if value > 35 else 0).getbbox()
    if bounds is None:
        raise ValueError("No signature ink found. Upload a clear signature on plain paper")
    # Many separated text lines indicate a document rather than a signature crop.
    mask = alpha.crop(bounds)
    sample = mask.copy()
    sample.thumbnail((500,500))
    groups = 0
    inside = False
    for y in range(sample.height):
        occupied = sum(value > 60 for value in sample.crop((0,y,sample.width,y+1)).getdata()) >= 2
        if occupied and not inside:
            groups += 1
        inside = occupied
    if groups > 8:
        raise ValueError("This image contains multiple lines of document text. Crop only the handwritten signature using Extract signature")
    image.putalpha(alpha)
    left, top, right, bottom = bounds
    padding = 4
    return image.crop((max(0,left-padding),max(0,top-padding),min(image.width,right+padding),min(image.height,bottom+padding)))


def signature_png(content):
    image = clean_signature_image(BytesIO(content))
    result = BytesIO()
    image.save(result,format="PNG")
    return result.getvalue()
