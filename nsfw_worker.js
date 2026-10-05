import * as tf from "https://esm.sh/@tensorflow/tfjs@4.22.0";
import * as nsfwjs from "https://esm.sh/nsfwjs@4.4.0?bundle";

let nsfwModel = null;
let downloadedBytes = 0;
const EXPECTED_TOTAL_BYTES = 30500000; // ~30.5MB for InceptionV3

const originalFetch = self.fetch;
self.fetch = async function(...args) {
    const response = await originalFetch(...args);
    if (!response.body) return response;
    
    // Only track model weights (skip small fetch calls if needed, but tracking all is fine)
    const reader = response.body.getReader();
    const stream = new ReadableStream({
        async start(controller) {
            while (true) {
                const { done, value } = await reader.read();
                if (done) {
                    controller.close();
                    break;
                }
                downloadedBytes += value.length;
                let p = (downloadedBytes / EXPECTED_TOTAL_BYTES) * 100;
                if (p > 99) p = 99; // Hold at 99% until tfjs actually finishes loading
                postMessage({ type: "progress", percent: p });
                
                controller.enqueue(value);
            }
        }
    });
    
    return new Response(stream, {
        headers: response.headers,
        status: response.status,
        statusText: response.statusText
    });
};

async function loadModel() {
    if (nsfwModel) return;
    try {
        await tf.ready();
        // Load the heavier, highly-trained InceptionV3 model
        nsfwModel = await nsfwjs.load("InceptionV3", { size: 299 });
        console.log("NSFW Worker: InceptionV3 model loaded successfully in background");
        
        // Warm up the model so the very first user upload doesn't stall
        try {
            const dummyImageData = new ImageData(299, 299);
            await nsfwModel.classify(dummyImageData);
            console.log("NSFW Worker: Model warmed up!");
        } catch (we) {
            console.warn("NSFW Worker warmup skipped:", we);
        }

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
