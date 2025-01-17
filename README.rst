Internet Archive PDF tools
##########################

:authors: - Merlijn Wajer <merlijn@archive.org>
:date: 2021-08-14 18:00

This repository contains a library to perform MRC (Mixed Raster Content)
compression on images [*]_, which offers lossy high compression of images, in
particular images with text.

Additionally, the library can generate MRC-compressed PDF files with hOCR [*]_
text layers mixed into to the PDF, which makes searching and copy-pasting of the
PDF possible. PDFs generated by `bin/recode_pdf` should be `PDF/A 3b` and
`PDF/UA` compatible.

Some of the tooling also supports specific Internet Archive file formats (such
as the "scandata.xml" files, but the tooling should work fine without those
files, too.

While the code is already being used internally to create PDFs at the Internet
Archive, the code still needs more documentation and cleaning up, so don't
expect this to be super well documented just yet.

Dependencies
============

* Python 3.x
* Python packages (also see `requirements.txt`):
    - PyMuPDF
    - lxml
    - scikit-image
    - Pillow
    - roman
    - `archive-hocr-tools <https://git.archive.org/merlijn/archive-hocr-tools>`_


One-of:

* `Kakadu JPEG2000 binaries <https://kakadusoftware.com/>`_
* Open source OpenJPEG2000 tools (opj_compress and opj_decompress)

Optional:

* For JBIG2 support, a (currently unreleased) version of mupdf is required.
  mupdf 1.19 is expected to fully support JBIG2. (PyMuPDF currently packages
  mupdf statically, so you'll have to make sure that version is also up to
  date). The default PDF compression options use ``ccitt``, so this is only
  required if you pass ``--jbig2`` to ``bin/pdf_recode``.


Features
========

* MRC compression of images, leading to anywhere from 3-15x compression ratios,
  depending on the quality setting provided.
* Creates PDF from a directory of images
* Improved compression based on OCR results (hOCR files)
* Hidden text layer insertion based on hOCR files, which makes a PDF searchable
  and the text copy-pasteable.
* PDF/A 3b compatible.
* Basic PDF/UA support (accessibility features)
* Support for optional denoising of masks to further improve compression
  (--denoise-mask)



Not well tested features
========================

* "Recoding" an existing PDF, extracting the images and creating a new PDF with
  the images from the existing PDF is not well tested. This works OK if every
  PDF page just has a single image.


Known issues
============

* Using ``--image-mode 0`` and ``--image-mode 1`` is currently broken, so only
  MRC or no images is supported.
* It is not possible to recode/compress a PDF without hOCR files. This will be
  addressed in the future, since it should not be a problem to generate lack
  hOCR data.


Planned features
================

* Support for using JPEG instead of JPEG2000 (faster PDF loading, but likely
  less compression)
* Addition of a second set of fonts in the PDFs, so that hidden selected text
  also renders the original glyphs.
* Faster partial blur

Features in progress
====================

* cupy (numpy/scipy on GPU) support


MRC
===

The goal of Mixed Raster Content compression is to decompose the image into a
background, foreground and mask. The background should contain components that
are not of particular interest, whereas the foreground would contain all
glyphs/text on a page, as well as the lines and edges of various drawings or
images. The mask is a 1-bit image which has the value '1' when a pixel is part
of the foreground.

This decomposition can then be used to compress the different components
individually, applying much higher compression to specific components, usually
the background, which can be downscaled as well. The foreground can be quite
compressed as well, since it mostly just needs to contain the approximate
colours of the text and other lines - any artifacts introduced during the
foreground compression (e.g. ugly artifact around text borders) are removed by
overlaying the mask component of the image, which is losslessly compressed
(typically using either JBIG2 or CCITT).

In a PDF, this usually means the background image is inserted into a page,
followed by the foreground image, which uses the mask as it's alpha layer.

Usage
-----

Scan a document, OCR it with Tesseract and save the result as a compressed PDF
(JPEG2000 compression with OpenJPEG, background downsamples three times), with
text layer::

    scanimage --resolution 300 --mode Color --format tiff | tee /tmp/scan.tiff | tesseract - - hocr > /tmp/scan.hocr ; recode_pdf -v --use-openjpeg --bg-downsample 3 --denoise-mask --from-imagestack /tmp/scan.tiff --hocr-file /tmp/scan.hocr -o /tmp/scan.pdf
    Page 1
         MMX
         SSE
         SSE2
         SSE3
         SSSE3
         SSE41
         POPCNT
         SSE42
         AVX
         F16C
    Creating text only PDF
    Starting page generation at 2021-03-05T00:22:59.294929
    Finished page generation at 2021-03-05T00:22:59.319370
    Creating text pages took 0.0245 seconds
    Inserting (and compressing) images
    Converting with image mode: 2
    Fixing up pymupdf metadata
    mupdf warnings, if any: ''
    Saving PDF now
    Processed 1 pages at 11.40 seconds/page
    Compression ratio: 249.876613


Examining the results
---------------------

Use ``pdfimages`` to extract the image layers of a specific page and then view
them with your favourite image viewer::

    pageno=0; pdfimages -f $pageno -l $pageno -png path_to_pdf extracted_image_base
    feh extracted_image_base*.png

License
=======

License for all code (minus ``internetarchive/pdfrenderer.py``) is AGPL 3.0.

``internetarchive/pdfrenderer.py`` is Apache 2.0, which matches the Tesseract
license for that file.


.. [*] https://en.wikipedia.org/wiki/Mixed_raster_content
.. [*] http://kba.cloud/hocr-spec/1.2/

