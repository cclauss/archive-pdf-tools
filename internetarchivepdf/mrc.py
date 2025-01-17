# Author: Merlijn Boris Wolf Wajer <merlijn@archive.org>

import sys
from os import close, remove

from glob import glob
from tempfile import mkstemp
import subprocess
from time import time

import warnings

from PIL import Image, ImageEnhance, ImageOps
from skimage.filters import threshold_local, threshold_otsu, threshold_sauvola
from skimage.restoration import denoise_tv_bregman, estimate_sigma

from scipy import ndimage
import numpy as np

from optimiser import optimise_gray, optimise_rgb, optimise_gray2, optimise_rgb2
from sauvola import binarise_sauvola

import fitz

fitz.TOOLS.set_icc(True) # For good measure, not required

from internetarchivepdf.const import (RECODE_RUNTIME_WARNING_TOO_SMALL_TO_DOWNSAMPLE,)


"""
"""

KDU_COMPRESS = 'kdu_compress'
KDU_EXPAND = 'kdu_expand'
OPJ_COMPRESS = 'opj_compress'
OPJ_DECOMPRESS = 'opj_decompress'


# skimage throws useless UserWarnings in various functions
def mean_estimate_sigma(arr):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        return np.mean(estimate_sigma(arr))


def threshold_image(img, rev=False, otsu=False, block_size=9):
    """
    Apply adaptive (local) thresholding, filtering out background noise to make
    the text more readable. 

    Returns the thresholded np image array
    """
    if otsu:
        try:
            binary_otsu = threshold_otsu(img)
        except ValueError:
            binary_otsu = np.ndarray(img.shape)
            binary_otsu[:] = 0

        if rev:
            binary_img = img > binary_otsu
        else:
            binary_img = img < binary_otsu
    else:
        #binary_local = threshold_local(img, block_size, method='gaussian')
        #binary_local = threshold_local(img, block_size, offset=10, method='gaussian')
        binary_local = threshold_local(img, block_size, method='gaussian')
        if not rev:
            binary_img = img < binary_local
        else:
            binary_img = img > binary_local

    return binary_img


def threshold_image2(np_image):
    local = threshold_image(np_image)
    otsu = threshold_image(np_image, otsu=True)

    return local & otsu


def threshold_image3(img):
    window_size = 51
    #window_size = 21

    h, w = img.shape
    out_img = np.ndarray(img.shape, dtype=np.bool)
    out_img = np.reshape(out_img, w*h)
    in_img = np.reshape(img, w*h)

    binarise_sauvola(in_img, out_img, w, h, window_size, window_size, 0.3, 128)
    out_img = np.reshape(out_img, (h, w))
    # TODO: optimise this, we can do it in binarise_sauvola
    out_img = np.invert(out_img)

    return out_img


def denoise_bregman(binary_img):
    thresf = np.array(binary_img, dtype=np.float32)
    #denoise = denoise_tv_bregman(thresf, weight=0.25)
    denoise = denoise_tv_bregman(thresf, weight=1.)

    #denoise = denoise > 0.6
    denoise = denoise > 0.4  # XXX: 0.4?
    denoise = np.array(denoise, dtype=np.bool)

    return denoise

# TODO: Rename, can be either foreground or background
def partial_blur(mask, img, sigma=5, mode=None):
    """
    Blur a part of the image 'img', where mask = 0.
    The actual values used by the blur are colours where mask = '1', effectively
    'erasing/blurring' parts of an image where mask = 0 with colours where mask = 1.

    At the end, restore all pixels from img where mask = 1.
    """
    maskf = np.array(mask, dtype=np.float32)

    if mode == 'RGB' or mode == 'RGBA':
        in_r = img[:, :, 0] * maskf
        in_g = img[:, :, 1] * maskf
        in_b = img[:, :, 2] * maskf
        filter_r = ndimage.filters.gaussian_filter(in_r, sigma = sigma)
        filter_g = ndimage.filters.gaussian_filter(in_g, sigma = sigma)
        filter_b = ndimage.filters.gaussian_filter(in_b, sigma = sigma)
    else:
        imgf = np.copy(img)
        imgf = np.array(imgf, dtype=np.float32)
        filter = ndimage.filters.gaussian_filter(imgf * maskf, sigma = sigma)

    weights = ndimage.filters.gaussian_filter(maskf, sigma = sigma)

    if mode == 'RGB' or mode == 'RGBA':
        filter_r /= weights + 0.00001
        filter_g /= weights + 0.00001
        filter_b /= weights + 0.00001

        newimg = np.copy(img)
        newimg[:, :, 0] = filter_r
        newimg[:, :, 1] = filter_g
        newimg[:, :, 2] = filter_b
    else:
        filter /= weights + 0.00001
        newimg = np.array(filter, dtype=np.uint8)

    newimg[mask] = img[mask]

    return newimg


def partial_boxblur(mask, fg, size=5, mode=None):
    maskf = np.array(mask, dtype=np.float32)

    if mode == 'RGB' or mode == 'RGBA':
        in_r = fg[:, :, 0] * maskf
        in_g = fg[:, :, 1] * maskf
        in_b = fg[:, :, 2] * maskf
        filter_r = ndimage.uniform_filter(in_r, size = size)
        filter_g = ndimage.uniform_filter(in_g, size = size)
        filter_b = ndimage.uniform_filter(in_b, size = size)
    else:
        fgf = np.copy(fg)
        fgf = np.array(fgf, dtype=np.float32)
        filter = ndimage.uniform_filter(fgf * maskf, size = size)

    weights = ndimage.uniform_filter(maskf, size = size)

    if mode == 'RGB' or mode == 'RGBA':
        filter_r /= weights + 0.00001
        filter_g /= weights + 0.00001
        filter_b /= weights + 0.00001

        newfg = np.copy(fg)
        newfg[:, :, 0] = filter_r
        newfg[:, :, 1] = filter_g
        newfg[:, :, 2] = filter_b
    else:
        filter /= weights + 0.00001
        newfg = np.array(filter, dtype=np.uint8)

    newfg[mask] = fg[mask]

    return newfg


def create_hocr_mask(img, mask_arr, hocr_word_data, downsample=None, timing_data=None):
    image_width, image_height = img.size
    np_img = np.array(img)

    t = time()
    for paragraphs in hocr_word_data:
        for lines in paragraphs['lines']:
            for word in lines['words']:
                if not word['text'].strip():
                    continue

                if downsample is not None:
                    left, top, right, bottom = [int(x/downsample) for x in word['bbox']]
                    # This can happen if we downsample and round to int
                    if left == right or top == bottom:
                        continue

                    np_wordimg = np_img[top:bottom,left:right]
                else:
                    left, top, right, bottom = [int(x) for x in word['bbox']]
                    np_wordimg = np_img[top:bottom,left:right]

                if (left >= right) or (top >= bottom):
                    print('Invalid bounding box: (%d, %d, %d, %d)' % (left, top, right, bottom), file=sys.stderr)
                    continue

                if (left < 0) or (right > image_width) or (top < 0) or (bottom > image_height):
                    print('Invalid bounding box outside image: (%d, %d, %d, %d)' % (left, top, right, bottom), file=sys.stderr)
                    continue

                thres = threshold_image2(np_wordimg)

                sigma_est = mean_estimate_sigma(thres)
                ones = np.count_nonzero(thres)

                if sigma_est > 0.1:
                    # Invert. (TODO: we should do this in a more efficient
                    # manner)
                    thres_i = threshold_image2(np.invert(np_wordimg))
                    sigma_est_i = mean_estimate_sigma(thres_i)
                    ones_i = np.count_nonzero(thres_i)

                    if sigma_est < sigma_est_i:
                        pass
                    elif sigma_est_i < sigma_est and ones_i < ones:
                        ones_i = np.count_nonzero(thres_i)

                        # Find what is closer to the center of the bounding box
                        ww, hh = thres.shape
                        center_x = ww/2
                        center_y = hh/2

                        thres_sum = 0.
                        thres_i_sum = 0.

                        # TODO: This can be done way more efficiently in numpy
                        for x in range(ww):
                            for y in range(hh):
                                if thres[x, y]:
                                    thres_sum += ((center_x-x)**2+(center_y-y)**2)**0.5
                                if thres_i[x, y]:
                                    thres_i_sum += ((center_x-y)**2+(center_y-y)**2)**0.5

                        if ones > 0:
                            thres_sum /= ones
                        if ones_i > 0:
                            thres_i_sum /= ones_i

                        if thres_sum < thres_i_sum:
                            pass
                        elif thres_i_sum > thres_sum:
                            thres = thres_i
                        else:
                            # Won't really ever happen, but ok
                            thres = thres_i | thres

                mask_arr[top:bottom, left:right] = thres

    if timing_data is not None:
        timing_data.append(('hocr_mask_gen', time() - t))


def create_threshold_mask(mask_arr, imgf, denoise_mask=None, timing_data=None):
    # We don't apply any of these blurs to the hOCR mask, we want that as
    # sharp as possible.

    t = time()
    sigma_est = mean_estimate_sigma(imgf)
    if timing_data is not None:
        timing_data.append(('est_1', time() - t))
    if sigma_est > 1.0:
        t = time()
        imgf = ndimage.filters.gaussian_filter(imgf, sigma=sigma_est*0.1)
        if timing_data is not None:
            timing_data.append(('blur_1', time() - t))

        #t = time()
        #n_sigma_est = mean_estimate_sigma(imgf)
        #time_data.append(('est_2', time() - t))
        #if sigma_est > 1.0 and n_sigma_est > 1.0:
        #    t = time()
        #    imgf = ndimage.filters.gaussian_filter(imgf, sigma=sigma_est*0.5)
        #    print('Going for second blur: n_sigma_est:',n_sigma_est)
        #    time_data.append(('blur_2', time() - t))

    t = time()
    #thres_arr = threshold_image3(np.array(imgf, dtype=np.uint8))
    thres_arr = threshold_image3(imgf.astype(np.uint8))
    if timing_data is not None:
        timing_data.append(('threshold', time() - t))

    if denoise_mask is not None and denoise_mask:
        t = time()
        sigma_est = mean_estimate_sigma(thres_arr)
        if timing_data is not None:
            timing_data.append(('est_3', time() - t))

        if sigma_est > 0.1:
            t = time()
            thres_arr = denoise_bregman(thres_arr)
            if timing_data is not None:
                timing_data.append(('denoise', time() - t))


    thres_inv = thres_arr ^ np.ones(thres_arr.shape, dtype=bool)

    mask_arr |= thres_arr


# TODO: Reduce amount of memory active at one given point (keep less images in
# memory, write to disk sooner, etc), careful with numpy <-> PIL conversions
def create_mrc_hocr_components(image, hocr_word_data,
                               downsample=None,
                               bg_downsample=None,
                               denoise_mask=None, timing_data=None,
                               errors=None):
    """
    Create the MRC components: mask, foreground and background

    Args:

    * image (PIL.Image): Image to be decomposed
    * hocr_word_data: OCR data about found text on the page
    * downsample (int): factor by which the OCR data is to be downsampled
    * bg_downsample (int): if the background image should be downscaled
    * denoise_mask (bool): Whether to denoise the image if it is deemed too
      noisy
    * timing_data: Optional timing data to log individual timing data to.
    * errors: Optional argument (of type set) with encountered runtime errors

    Returns a tuple of the components, as numpy arrays: (mask, foreground,
    background)
    """
    grayimg = image
    if image.mode != 'L':
        t = time()
        grayimg = image.convert('L')
        if timing_data is not None:
            timing_data.append(('grey_conversion', time() - t))

    mask_arr = np.array(Image.new('1', image.size))

    # Modifies mask_arr in place
    create_hocr_mask(grayimg, mask_arr, hocr_word_data, downsample=downsample,
                     timing_data=timing_data)

    grayimgf = np.array(grayimg, dtype=np.float32)

    MIX_THRESHOLD = True
    if MIX_THRESHOLD:
        # Modifies mask_arr in place
        #mask_arr = np.zeros(mask_arr.shape, dtype=np.bool) # XXX: this nukes the hocr threshold
        create_threshold_mask(mask_arr, grayimgf, denoise_mask=denoise_mask,
                timing_data=timing_data)
    yield mask_arr

    image_arr = np.array(image)

    t = time()
    # Take foreground pixels and optimise the image by making the surrounding
    # pixels like the foreground, allowing for more optimal compression (and
    # higher quality foreground pixels as a result)
    width_, height_ = image.size
    if image.mode == 'L':
        foreground_arr = optimise_gray2(mask_arr, image_arr, width_, height_, 3)
    else:
        foreground_arr = optimise_rgb2(mask_arr, image_arr, width_, height_, 3)
    if timing_data is not None:
        # The name fg_partial_blur is kept for backwards compatibility
        timing_data.append(('fg_partial_blur', time() - t))
    yield foreground_arr
    foreground_arr = None

    mask_inv = mask_arr ^ np.ones(mask_arr.shape, dtype=bool)

    t = time()
    # Take background pixels and optimise the image by placing them where the
    # foreground pixels are thought to be, this has the effect of reducing
    # compression artifacts (thus improving quality) and at the same time making
    # the image easier to compress (smaller file size)
    if image.mode == 'L':
        background_arr = optimise_gray2(mask_inv, image_arr, width_, height_, 10)
    else:
        background_arr = optimise_rgb2(mask_inv, image_arr, width_, height_, 10)
    if timing_data is not None:
        # The name bg_partial_blur is kept for backwards compatibility
        timing_data.append(('bg_partial_blur', time() - t))

    if bg_downsample is not None:
        t = time()
        image2 = Image.fromarray(background_arr)
        w, h = image2.size
        w_downsample = int(w / bg_downsample)
        h_downsample = int(h / bg_downsample)
        if w_downsample > 0 and h_downsample > 0:
            image2.thumbnail((w_downsample, h_downsample))
            background_arr = np.array(image2)
        else:
            if errors is not None:
                errors.add(RECODE_RUNTIME_WARNING_TOO_SMALL_TO_DOWNSAMPLE)

        if timing_data is not None:
            timing_data.append(('bg_downsample', time() - t))

    yield background_arr
    return


def encode_mrc_mask(np_mask, tmp_dir=None, jbig2=True, timing_data=None):
    """
    Encode mask image either to JBIG2 or PNG.

    Args:

    * np_mask (numpy.array): Mask image array
    * tmp_dir (str): path the temporary directory to write images to
    * jbig2 (bool): Whether to encode to JBIG2 or PNG
    * timing_data (optional): Add time information to timing_data structure

    Returns a tuple: (str, str) where the first entry is the jbig2
    path, if any, the second is the png path.
    """
    t = time()
    mask = Image.fromarray(np_mask)

    fd, mask_img_png = mkstemp(prefix='mask', suffix='.png', dir=tmp_dir)
    close(fd)
    if jbig2:
        fd, mask_img_jbig2 = mkstemp(prefix='mask', suffix='.jbig2', dir=tmp_dir)
        close(fd)

    mask.save(mask_img_png, compress_level=0)

    if jbig2:
        out = subprocess.check_output(['jbig2', mask_img_png])
        fp= open(mask_img_jbig2, 'wb+')
        fp.write(out)
        fp.close()

    if timing_data is not None:
        timing_data.append(('mask_jbig2', time()-t))

    if jbig2:
        return mask_img_jbig2, mask_img_png
    else:
        return None, mask_img_png


def encode_mrc_background(np_bg, bg_slope, tmp_dir=None, use_kdu=True, timing_data=None):
    """
    Encode background image as JPEG2000, with the provided compression settings
    and JPEG2000 encoder.

    Args:

    * np_bg (numpy.array): Background image array
    * bg_slope (int): Compression parameter(s), WIP
    * tmp_dir (str): path the temporary directory to write images to
    * use_kdu (bool): Whether to encode using Kakadu or OpenJPEG2000
    * timing_data (optional): Add time information to timing_data structure

    Returns the filepath to the JPEG2000 background image
    """
    t = time()
    # Create background
    if use_kdu:
        # TODO: check if kakadu supports .tif
        fd, bg_img_tiff = mkstemp(prefix='bg', suffix='.tiff', dir=tmp_dir)
    else:
        fd, bg_img_tiff = mkstemp(prefix='bg', suffix='.pnm', dir=tmp_dir)
    close(fd)
    fd, bg_img_jp2 = mkstemp(prefix='bg', suffix='.jp2', dir=tmp_dir)
    close(fd)
    remove(bg_img_jp2) # XXX: Kakadu doesn't want the file to exist, so what are
                       # we even doing

    bg_img = Image.fromarray(np_bg)
    bg_img.save(bg_img_tiff)

    if use_kdu:
        subprocess.check_call([KDU_COMPRESS,
            '-num_threads', '0',
            '-i', bg_img_tiff, '-o', bg_img_jp2,
            '-slope', str(bg_slope),
            ], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    else:
        subprocess.check_call([OPJ_COMPRESS,
            '-i', bg_img_tiff, '-o', bg_img_jp2,
            '-threads', '1',
            # Use constant reduction rate here (not psnr)
            '-r', '400',
            ], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    remove(bg_img_tiff)

    if timing_data is not None:
        timing_data.append(('bg_jp2', time()-t))

    return bg_img_jp2


def encode_mrc_foreground(np_fg, fg_slope, tmp_dir=None, use_kdu=True, timing_data=None):
    """
    Encode foreground image as JPEG2000, with the provided compression settings
    and JPEG2000 encoder.

    Args:

    * np_bg (numpy.array): Foreground image array
    * bg_slope (int): Compression parameter(s), WIP
    * tmp_dir (str): path the temporary directory to write images to
    * use_kdu (bool): Whether to encode using Kakadu or OpenJPEG2000
    * timing_data (optional): Add time information to timing_data structure

    Returns the filepath to the JPEG2000 foreground image
    """
    t = time()
    # Create foreground
    if use_kdu:
        # TODO: check if kakadu supports .tif
        fd, fg_img_tiff = mkstemp(prefix='fg', suffix='.tiff', dir=tmp_dir)
    else:
        fd, fg_img_tiff = mkstemp(prefix='fg', suffix='.pnm', dir=tmp_dir)

    close(fd)
    fd, fg_img_jp2 = mkstemp(prefix='fg', suffix='.jp2', dir=tmp_dir)
    close(fd)
    remove(fg_img_jp2) # XXX: Kakadu doesn't want the file to exist, so what are
                       # we even doing

    fg_img = Image.fromarray(np_fg)
    fg_img.save(fg_img_tiff)

    if use_kdu:
        subprocess.check_call([KDU_COMPRESS,
            '-num_threads', '0',
            '-i', fg_img_tiff, '-o', fg_img_jp2,
            '-slope', str(fg_slope),
            ], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    else:
        subprocess.check_call([OPJ_COMPRESS,
            '-threads', '1',
            '-i', fg_img_tiff, '-o', fg_img_jp2,
            # Use PSNR here
            '-q', '25',
            ], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    remove(fg_img_tiff)

    if timing_data is not None:
        timing_data.append(('fg_jp2', time()-t))

    return fg_img_jp2


def encode_mrc_images(mrc_gen, bg_slope=None, fg_slope=None,
                      tmp_dir=None, jbig2=True, timing_data=None, use_kdu=True):
    mask_img_jbig2, mask_img_png = encode_mrc_mask(next(mrc_gen), tmp_dir=tmp_dir, jbig2=jbig2,
            timing_data=timing_data)

    np_fg = next(mrc_gen)
    fg_img_jp2 = encode_mrc_foreground(np_fg, fg_slope, tmp_dir=tmp_dir,
                                       use_kdu=use_kdu, timing_data=timing_data)
    fg_h, fg_w = np_fg.shape[0:2]
    np_fg = None

    np_bg = next(mrc_gen)
    bg_img_jp2 = encode_mrc_background(np_bg, bg_slope, tmp_dir=tmp_dir,
                                       use_kdu=use_kdu, timing_data=timing_data)
    bg_h, bg_w = np_bg.shape[0:2]
    np_bg = None

    # XXX: probably don't need this
    try:
        _ = next(mrc_gen)
    except StopIteration:
        pass

    if jbig2:
        remove(mask_img_png)

    if jbig2:
        return mask_img_jbig2, bg_img_jp2, (bg_w, bg_h), fg_img_jp2, (fg_w, fg_h)
    else:
        # Return PNG which mupdf will turn into ccitt with
        # save(..., deflate=True) until mupdf fixes their JBIG2 support
        #return mask_img_png, bg_img_jp2, fg_img_jp2
        return mask_img_png, bg_img_jp2, (bg_w, bg_h), fg_img_jp2, (fg_w, fg_h)
