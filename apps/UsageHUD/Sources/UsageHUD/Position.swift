import CoreGraphics

func notchAnchoredOrigin(size: CGSize, screenFrame: CGRect) -> CGPoint {
    CGPoint(
        x: screenFrame.midX - size.width / 2,
        y: screenFrame.maxY - size.height
    )
}
