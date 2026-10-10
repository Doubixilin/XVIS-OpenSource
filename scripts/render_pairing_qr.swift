import Foundation
import CoreImage
import ImageIO
import UniformTypeIdentifiers

// The short-lived scan capability comes through stdin, never a process argument.
let data = FileHandle.standardInput.readDataToEndOfFile()
guard CommandLine.arguments.count == 2,
      let filter = CIFilter(name: "CIQRCodeGenerator") else { exit(1) }
filter.setValue(data, forKey: "inputMessage")
filter.setValue("M", forKey: "inputCorrectionLevel")
guard let output = filter.outputImage?.transformed(by: CGAffineTransform(scaleX: 8, y: 8)),
      let image = CIContext().createCGImage(output, from: output.extent),
      let destination = CGImageDestinationCreateWithURL(URL(fileURLWithPath: CommandLine.arguments[1]) as CFURL,
          UTType.png.identifier as CFString, 1, nil) else { exit(1) }
CGImageDestinationAddImage(destination, image, nil)
guard CGImageDestinationFinalize(destination) else { exit(1) }
