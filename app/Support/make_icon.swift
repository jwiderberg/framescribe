// Draws the app icon (1024 x 1024 PNG) with CoreGraphics: a rounded square, a film strip and a speech waveform.
// No artwork is downloaded. Usage: swift make_icon.swift OUT.png   (build_app.sh turns it into AppIcon.icns)

import AppKit
import CoreGraphics
import Foundation

let size = 1024
let outPath = CommandLine.arguments.dropFirst().first ?? "icon_1024.png"
let space = CGColorSpace(name: CGColorSpace.sRGB)!
guard let ctx = CGContext(data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: 0, space: space,
                          bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
else { fatalError("no context") }

func color(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> CGColor {
    CGColor(colorSpace: space, components: [r, g, b, a])!
}

// background: macOS icon grid, an 824 pt rounded square centred in 1024
let inset: CGFloat = 100
let body = CGRect(x: inset, y: inset, width: CGFloat(size) - 2 * inset, height: CGFloat(size) - 2 * inset)
let rounded = CGPath(roundedRect: body, cornerWidth: 185, cornerHeight: 185, transform: nil)
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -12), blur: 28, color: color(0, 0, 0, 0.35))
ctx.addPath(rounded)
ctx.setFillColor(color(0.10, 0.36, 0.85))
ctx.fillPath()
ctx.restoreGState()
ctx.saveGState()
ctx.addPath(rounded)
ctx.clip()
let gradient = CGGradient(colorsSpace: space, colors: [color(0.20, 0.55, 1.0), color(0.08, 0.27, 0.72)] as CFArray,
                          locations: [0, 1])!
ctx.drawLinearGradient(gradient, start: CGPoint(x: 0, y: body.maxY), end: CGPoint(x: 0, y: body.minY), options: [])
ctx.restoreGState()

// film strip across the upper half
let strip = CGRect(x: body.minX + 70, y: 560, width: body.width - 140, height: 210)
ctx.setFillColor(color(1, 1, 1, 0.95))
ctx.addPath(CGPath(roundedRect: strip, cornerWidth: 28, cornerHeight: 28, transform: nil))
ctx.fillPath()
ctx.setFillColor(color(0.10, 0.33, 0.80))
let holes = 9
let holeW: CGFloat = 34, holeH: CGFloat = 26
for i in 0..<holes {
    let x = strip.minX + 30 + CGFloat(i) * (strip.width - 60 - holeW) / CGFloat(holes - 1)
    for y in [strip.minY + 16, strip.maxY - 16 - holeH] {
        ctx.addPath(CGPath(roundedRect: CGRect(x: x, y: y, width: holeW, height: holeH), cornerWidth: 6, cornerHeight: 6, transform: nil))
    }
}
ctx.fillPath()
// three frames inside the strip
let frameY = strip.minY + 56, frameH = strip.height - 112
let frameW = (strip.width - 60 - 2 * 24) / 3
for i in 0..<3 {
    let r = CGRect(x: strip.minX + 30 + CGFloat(i) * (frameW + 24), y: frameY, width: frameW, height: frameH)
    ctx.setFillColor(color(0.16, 0.45, 0.95, CGFloat(0.55 + 0.15 * Double(i))))
    ctx.addPath(CGPath(roundedRect: r, cornerWidth: 10, cornerHeight: 10, transform: nil))
    ctx.fillPath()
}

// speech waveform below
let bars: [CGFloat] = [0.25, 0.5, 0.8, 0.55, 1.0, 0.7, 0.4, 0.85, 0.6, 0.3, 0.5, 0.2]
let barW: CGFloat = 34, gap: CGFloat = 22
let totalW = CGFloat(bars.count) * barW + CGFloat(bars.count - 1) * gap
let centreY: CGFloat = 360, maxH: CGFloat = 250
ctx.setFillColor(color(1, 1, 1))
for (i, h) in bars.enumerated() {
    let x = CGFloat(size) / 2 - totalW / 2 + CGFloat(i) * (barW + gap)
    let height = max(barW, maxH * h)
    ctx.addPath(CGPath(roundedRect: CGRect(x: x, y: centreY - height / 2, width: barW, height: height),
                       cornerWidth: barW / 2, cornerHeight: barW / 2, transform: nil))
}
ctx.fillPath()

guard let image = ctx.makeImage() else { fatalError("no image") }
let rep = NSBitmapImageRep(cgImage: image)
guard let png = rep.representation(using: .png, properties: [:]) else { fatalError("no png") }
try png.write(to: URL(fileURLWithPath: outPath))
print("wrote \(outPath)")
