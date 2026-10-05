import * as tf from "https://esm.sh/@tensorflow/tfjs@4.22.0";
import * as nsfwjs from "https://esm.sh/nsfwjs@4.4.0?bundle";

let nsfwModel = null;

async function loadModel() {
    if (nsfwModel) return;
    try {
        await tf.ready();
        // Load the heavier, highly-trained InceptionV3 model
        nsfwModel = await nsfwjs.load("InceptionV3", { size: 299 });
        console.log("NSFW Worker: InceptionV3 model loaded successfully in background");
        postMessage({ type: "modelLoaded" });
    } catch (e) {
        console.error("NSFW Worker model load error:", e);
    }
}

self.onmessage = async (e) => {
    const { id, type, imageBitmap } = e.data;
    
    if (type === "init") {
        await loadModel();
        return;
    }
    
    if (type === "classify") {
        try {
            await loadModel();
            const predictions = await nsfwModel.classify(imageBitmap);
            
            const porn = predictions.find((x) => x.className === "Porn")?.probability || 0;
            const hentai = predictions.find((x) => x.className === "Hentai")?.probability || 0;
            const sexy = predictions.find((x) => x.className === "Sexy")?.probability || 0;
            
            const unsafe = Math.max(porn, hentai);
            // Strict threshold for porn/hentai, higher threshold for sexy to reduce false positives
            const isNSFW = unsafe >= 0.65 || sexy >= 0.90;
            
            postMessage({ type: "result", id, isNSFW, predictions });
            
            // Clean up
            if (imageBitmap && imageBitmap.close) {
                imageBitmap.close();
            }
        } catch (err) {
            console.error("NSFW Worker classify error:", err);
            postMessage({ type: "error", id, error: err.toString() });
            if (imageBitmap && imageBitmap.close) {
                imageBitmap.close();
            }
        }
    }
};
