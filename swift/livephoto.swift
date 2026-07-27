// 生成 iPhone 实况照片 (Live Photo) 的配对文件。
//
// 实况照片 = 静态图 + 短视频,靠一个 UUID 绑定:
//   静态图: EXIF MakerApple 第 17 号键 = UUID
//   视频:   com.apple.quicktime.content.identifier = 同一个 UUID
//           外加一条 com.apple.quicktime.still-image-time 元数据轨,标出静态图对应的时刻
// 这套字段是从本机图库里真实的实况照片上逆向确认的,不是猜的。
//
// 用法: livephoto --still S.jpg --video V.mov --out-dir DIR [--uuid U] [--still-time SEC]
//                [--width W --height H]

import AVFoundation
import CoreServices
import Foundation
import ImageIO

let STILL_IMAGE_TIME_KEY = "com.apple.quicktime.still-image-time"
let CONTENT_ID_KEY = "com.apple.quicktime.content.identifier"
let MAKER_APPLE_CONTENT_ID = 17

struct Args {
    var still = "", video = "", outDir = ""
    var uuid = UUID().uuidString
    var stillTime = 0.0
    var width = 0, height = 0   // 0 = 不缩放,保持原图尺寸
}

func parseArgs() -> Args {
    var a = Args()
    var it = CommandLine.arguments.dropFirst().makeIterator()
    while let flag = it.next() {
        let val: () -> String = {
            guard let v = it.next() else {
                FileHandle.standardError.write("缺少 \(flag) 的值\n".data(using: .utf8)!)
                exit(2)
            }
            return v
        }
        switch flag {
        case "--still":      a.still = val()
        case "--video":      a.video = val()
        case "--out-dir":    a.outDir = val()
        case "--uuid":       a.uuid = val()
        case "--still-time": a.stillTime = Double(val()) ?? 0
        case "--width":      a.width  = Int(val()) ?? 0
        case "--height":     a.height = Int(val()) ?? 0
        default:
            FileHandle.standardError.write("未知参数 \(flag)\n".data(using: .utf8)!)
            exit(2)
        }
    }
    guard !a.still.isEmpty, !a.video.isEmpty, !a.outDir.isEmpty else {
        FileHandle.standardError.write(
            "用法: livephoto --still S --video V --out-dir D\n".data(using: .utf8)!)
        exit(2)
    }
    return a
}

func die(_ msg: String) -> Never {
    FileHandle.standardError.write("错误: \(msg)\n".data(using: .utf8)!)
    exit(1)
}

/// 把 UUID 写进静态图的 MakerApple[17],完整保留源图所有 EXIF。
/// 若给了 width/height,用 thumbnail API 缩放(自动应用 EXIF 旋转),否则原尺寸。
func writeStill(from src: String, to dst: String, uuid: String, width: Int, height: Int) {
    guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: src) as CFURL, nil) else {
        die("读不了静态图 \(src)")
    }

    // 读取全部原始属性(含 Exif/GPS/MakerApple 等)
    var meta = (CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [String: Any]) ?? [:]

    // 注入 MakerApple[17] = UUID (Live Photo 配对键)
    var maker = (meta["{MakerApple}"] as? [String: Any]) ?? [:]
    maker["\(MAKER_APPLE_CONTENT_ID)"] = uuid
    meta["{MakerApple}"] = maker
    // 不覆盖压缩质量 —— 让 ImageIO 按源文件质量保存

    let dstURL = URL(fileURLWithPath: dst) as CFURL
    guard let dest = CGImageDestinationCreateWithURL(dstURL, UTType.jpeg.identifier as CFString, 1, nil)
    else { die("建不了输出静态图 \(dst)") }

    // maxSide=0 (未指定 width/height) 时用一个远大于实际尺寸的值,相当于不限制/不缩放
    let maxSide = (width > 0 && height > 0) ? max(width, height) : 100_000
    let thumbOpts: [CFString: Any] = [
        kCGImageSourceThumbnailMaxPixelSize: maxSide,
        kCGImageSourceCreateThumbnailWithTransform: true,   // 自动 bake EXIF 旋转进像素
        kCGImageSourceCreateThumbnailFromImageAlways: true,
    ]
    guard let img = CGImageSourceCreateThumbnailAtIndex(source, 0, thumbOpts as CFDictionary)
    else { die("静态图处理失败 \(src)") }
    // 旋转已 bake 进像素,把 Orientation 标签改为 1(正向),避免播放器/Photos 二次旋转
    meta[kCGImagePropertyOrientation as String] = 1
    if var tiff = meta["{TIFF}"] as? [String: Any] {
        tiff["Orientation"] = 1
        meta["{TIFF}"] = tiff
    }
    CGImageDestinationAddImage(dest, img, meta as CFDictionary)

    guard CGImageDestinationFinalize(dest) else { die("写静态图失败 \(dst)") }
}

/// 造一条只含 still-image-time 的元数据轨的格式描述。
func stillImageTimeFormatDescription() -> CMFormatDescription {
    let spec: [String: Any] = [
        kCMMetadataFormatDescriptionMetadataSpecificationKey_Identifier as String:
            "mdta/\(STILL_IMAGE_TIME_KEY)",
        kCMMetadataFormatDescriptionMetadataSpecificationKey_DataType as String:
            kCMMetadataBaseDataType_SInt8 as String,
    ]
    var desc: CMFormatDescription?
    let status = CMMetadataFormatDescriptionCreateWithMetadataSpecifications(
        allocator: kCFAllocatorDefault,
        metadataType: kCMMetadataFormatType_Boxed,
        metadataSpecifications: [spec] as CFArray,
        formatDescriptionOut: &desc)
    guard status == noErr, let d = desc else { die("建 still-image-time 轨失败 (\(status))") }
    return d
}

func stillImageTimeItem() -> AVMutableMetadataItem {
    let item = AVMutableMetadataItem()
    item.identifier = AVMetadataItem.identifier(
        forKey: STILL_IMAGE_TIME_KEY, keySpace: .quickTimeMetadata)
    item.dataType = kCMMetadataBaseDataType_SInt8 as String
    item.value = 0 as NSNumber  // 0 = 这一刻就是静态图对应的帧
    return item
}

func contentIdItem(_ uuid: String) -> AVMutableMetadataItem {
    let item = AVMutableMetadataItem()
    item.identifier = AVMetadataItem.identifier(
        forKey: CONTENT_ID_KEY, keySpace: .quickTimeMetadata)
    item.dataType = kCMMetadataBaseDataType_UTF8 as String
    item.value = uuid as NSString
    return item
}

/// 原样搬运视频/音频轨 (不重编码),加上 content.identifier 和 still-image-time 轨。
func writeVideo(from src: String, to dst: String, uuid: String, stillTime: Double) async {
    // 先写临时文件,成功后才替换 dst —— dst 和 src 在大小写不敏感的文件系统上
    // 可能是同一个文件 (如 foo.mov vs foo.MOV),提前删 dst 会把还没读的 src 删掉
    let tmpDst = dst + ".tmp-\(UUID().uuidString).mov"
    let asset = AVURLAsset(url: URL(fileURLWithPath: src))

    guard let reader = try? AVAssetReader(asset: asset) else { die("读不了视频 \(src)") }
    guard let writer = try? AVAssetWriter(outputURL: URL(fileURLWithPath: tmpDst), fileType: .mov)
    else { die("建不了输出视频 \(tmpDst)") }

    writer.metadata = [contentIdItem(uuid)]

    // 1) 原样搬运已有的音视频轨
    var pairs: [(AVAssetReaderTrackOutput, AVAssetWriterInput)] = []
    let tracks: [AVAssetTrack]
    do {
        tracks = try await asset.loadTracks(withMediaType: .video)
            + (try await asset.loadTracks(withMediaType: .audio))
    } catch { die("读轨道失败: \(error)") }
    guard !tracks.isEmpty else { die("视频里没有可用轨道") }

    for t in tracks {
        let out = AVAssetReaderTrackOutput(track: t, outputSettings: nil)  // nil = 不解码
        guard reader.canAdd(out) else { die("加不了读取轨 \(t.trackID)") }
        reader.add(out)
        let hint = try? await t.load(.formatDescriptions).first
        let inp = AVAssetWriterInput(
            mediaType: t.mediaType, outputSettings: nil, sourceFormatHint: hint ?? nil)
        inp.expectsMediaDataInRealTime = false
        guard writer.canAdd(inp) else { die("加不了写入轨 \(t.trackID)") }
        writer.add(inp)
        pairs.append((out, inp))
    }

    // 2) still-image-time 元数据轨
    let metaInput = AVAssetWriterInput(
        mediaType: .metadata, outputSettings: nil,
        sourceFormatHint: stillImageTimeFormatDescription())
    metaInput.expectsMediaDataInRealTime = false
    let adaptor = AVAssetWriterInputMetadataAdaptor(assetWriterInput: metaInput)
    guard writer.canAdd(metaInput) else { die("加不了 still-image-time 轨") }
    writer.add(metaInput)

    guard writer.startWriting() else { die("开始写入失败: \(writer.error?.localizedDescription ?? "?")") }
    guard reader.startReading() else { die("开始读取失败: \(reader.error?.localizedDescription ?? "?")") }
    writer.startSession(atSourceTime: .zero)

    // still-image-time 标在指定时刻,给一个很短的持续时间
    let t = CMTime(seconds: max(0, stillTime), preferredTimescale: 600)
    let group = AVTimedMetadataGroup(
        items: [stillImageTimeItem()],
        timeRange: CMTimeRange(start: t, duration: CMTime(value: 1, timescale: 600)))
    guard adaptor.append(group) else { die("写 still-image-time 失败") }
    metaInput.markAsFinished()

    // 3) 搬运样本
    for (out, inp) in pairs {
        let q = DispatchQueue(label: "copy.\(inp.mediaType.rawValue)")
        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            inp.requestMediaDataWhenReady(on: q) {
                while inp.isReadyForMoreMediaData {
                    guard let sb = out.copyNextSampleBuffer() else {
                        inp.markAsFinished()
                        cont.resume()
                        return
                    }
                    if !inp.append(sb) {
                        inp.markAsFinished()
                        cont.resume()
                        return
                    }
                }
            }
        }
    }

    await writer.finishWriting()
    if writer.status != .completed {
        try? FileManager.default.removeItem(atPath: tmpDst)
        die("写视频失败: \(writer.error?.localizedDescription ?? "未知")")
    }
    if reader.status == .failed {
        try? FileManager.default.removeItem(atPath: tmpDst)
        die("读视频失败: \(reader.error?.localizedDescription ?? "未知")")
    }
    // 写成功后原子替换：先删 dst（此时 src 已读完，删 dst 安全）
    try? FileManager.default.removeItem(atPath: dst)
    do {
        try FileManager.default.moveItem(atPath: tmpDst, toPath: dst)
    } catch {
        die("移动临时视频失败: \(error)")
    }
}

// ---- main ----
let args = parseArgs()
let outDir = URL(fileURLWithPath: args.outDir)
try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)

let base = URL(fileURLWithPath: args.video).deletingPathExtension().lastPathComponent
let stillOut = outDir.appendingPathComponent("\(base).JPG").path
let videoOut = outDir.appendingPathComponent("\(base).MOV").path

writeStill(from: args.still, to: stillOut, uuid: args.uuid, width: args.width, height: args.height)

let sem = DispatchSemaphore(value: 0)
Task {
    await writeVideo(from: args.video, to: videoOut, uuid: args.uuid, stillTime: args.stillTime)
    sem.signal()
}
sem.wait()

print("uuid=\(args.uuid)")
print("still=\(stillOut)")
print("video=\(videoOut)")
