"""Print a URL as a QR code in the terminal:  python scripts/print_qr.py <url>"""
import sys

import qrcode

qr = qrcode.QRCode(border=2)
qr.add_data(sys.argv[1])
qr.print_ascii(invert=True)      # invert=True: dark modules drawn as filled blocks on dark terminals
