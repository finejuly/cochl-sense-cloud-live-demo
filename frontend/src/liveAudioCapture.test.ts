import { afterEach, describe, expect, it, vi } from 'vitest';
import { createLiveAudioCapture } from './liveAudio';

describe('createLiveAudioCapture', () => {
  const originalAudioContext = window.AudioContext;
  const originalAudioWorkletNode = globalThis.AudioWorkletNode;

  afterEach(() => {
    Object.defineProperty(window, 'AudioContext', {
      configurable: true,
      value: originalAudioContext,
    });
    Object.defineProperty(globalThis, 'AudioWorkletNode', {
      configurable: true,
      value: originalAudioWorkletNode,
    });
    vi.restoreAllMocks();
  });

  it('uses AudioWorklet for capture and releases all audio resources', async () => {
    class FakeNode {
      connect = vi.fn();
      disconnect = vi.fn();
    }

    class FakePort {
      onmessage: ((event: MessageEvent<Float32Array>) => void) | null = null;
      close = vi.fn();
    }

    let context: FakeAudioContext | null = null;
    let worklet: FakeAudioWorkletNode | null = null;

    class FakeAudioWorkletNode extends FakeNode {
      port = new FakePort();

      constructor() {
        super();
        worklet = this;
      }
    }

    class FakeAudioContext {
      sampleRate = 4;
      destination = new FakeNode();
      source = new FakeNode();
      analyser = Object.assign(new FakeNode(), {
        fftSize: 128,
        frequencyBinCount: 64,
        getByteFrequencyData: vi.fn(),
      });
      audioWorklet = { addModule: vi.fn(async () => undefined) };
      close = vi.fn(async () => undefined);

      constructor() {
        context = this;
      }

      createMediaStreamSource() {
        return this.source;
      }

      createAnalyser() {
        return this.analyser;
      }
    }

    Object.defineProperty(window, 'AudioContext', {
      configurable: true,
      value: FakeAudioContext,
    });
    Object.defineProperty(globalThis, 'AudioWorkletNode', {
      configurable: true,
      value: FakeAudioWorkletNode,
    });
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:worklet');
    const revokeSpy = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const onWindow = vi.fn();

    const cleanup = await createLiveAudioCapture({} as MediaStream, onWindow, {
      windowSec: 2,
      hopSec: 1,
    });

    expect(context?.audioWorklet.addModule).toHaveBeenCalledWith('blob:worklet');
    expect(revokeSpy).toHaveBeenCalledWith('blob:worklet');
    worklet?.port.onmessage?.({ data: Float32Array.from([0, 1, 2, 3, 4, 5, 6, 7]) } as MessageEvent<Float32Array>);
    expect(onWindow).toHaveBeenCalledTimes(1);

    cleanup();

    expect(worklet?.port.close).toHaveBeenCalled();
    expect(context?.source.disconnect).toHaveBeenCalled();
    expect(context?.analyser.disconnect).toHaveBeenCalled();
    expect(worklet?.disconnect).toHaveBeenCalled();
    expect(context?.close).toHaveBeenCalled();
  });

  it('requires the AudioContext to honor an explicitly requested sample rate', async () => {
    const close = vi.fn(async () => undefined);
    const constructorOptions: Array<AudioContextOptions | undefined> = [];

    class FakeAudioContext {
      sampleRate = 44_100;
      close = close;

      constructor(options?: AudioContextOptions) {
        constructorOptions.push(options);
      }
    }

    Object.defineProperty(window, 'AudioContext', {
      configurable: true,
      value: FakeAudioContext,
    });

    await expect(createLiveAudioCapture({} as MediaStream, vi.fn(), {
      sampleRate: 48_000,
    })).rejects.toThrow('48000 Hz 오디오 처리를 지원하지 않는 환경입니다.');

    expect(constructorOptions).toEqual([{ sampleRate: 48_000 }]);
    expect(close).toHaveBeenCalledTimes(1);
  });

  it('resumes a suspended context and reports audio time and later state changes', async () => {
    class FakeNode {
      connect = vi.fn();
      disconnect = vi.fn();
    }

    let context: FakeAudioContext | null = null;
    let processor: FakeNode & {
      onaudioprocess: ((event: AudioProcessingEvent) => void) | null;
    } | null = null;

    class FakeAudioContext {
      sampleRate = 4;
      state: AudioContextState = 'suspended';
      destination = new FakeNode();
      source = new FakeNode();
      analyser = Object.assign(new FakeNode(), {
        fftSize: 128,
        frequencyBinCount: 64,
        getByteFrequencyData: vi.fn(),
      });
      close = vi.fn(async () => {
        this.state = 'closed';
      });
      resume = vi.fn(async () => {
        this.state = 'running';
      });
      private stateListeners = new Set<EventListenerOrEventListenerObject>();

      constructor() {
        context = this;
      }

      createMediaStreamSource() {
        return this.source;
      }

      createAnalyser() {
        return this.analyser;
      }

      createScriptProcessor() {
        processor = Object.assign(new FakeNode(), { onaudioprocess: null });
        return processor;
      }

      addEventListener(_type: string, listener: EventListenerOrEventListenerObject) {
        this.stateListeners.add(listener);
      }

      removeEventListener(_type: string, listener: EventListenerOrEventListenerObject) {
        this.stateListeners.delete(listener);
      }

      dispatchStateChange() {
        const event = new Event('statechange');
        this.stateListeners.forEach((listener) => {
          if (typeof listener === 'function') {
            listener(event);
          } else {
            listener.handleEvent(event);
          }
        });
      }
    }

    Object.defineProperty(window, 'AudioContext', {
      configurable: true,
      value: FakeAudioContext,
    });
    Object.defineProperty(globalThis, 'AudioWorkletNode', {
      configurable: true,
      value: undefined,
    });
    const onAudioTimeUpdate = vi.fn();
    const onStateChange = vi.fn();

    const controller = await createLiveAudioCapture({} as MediaStream, vi.fn(), {
      onAudioTimeUpdate,
      onStateChange,
    });

    expect(context?.resume).toHaveBeenCalledTimes(1);
    expect(onStateChange).toHaveBeenLastCalledWith('running');
    processor?.onaudioprocess?.({
      inputBuffer: { getChannelData: () => Float32Array.from([0, 0, 0, 0]) },
    } as AudioProcessingEvent);
    expect(onAudioTimeUpdate).toHaveBeenCalledWith(1);

    if (!context) {
      throw new Error('Expected audio context');
    }
    context.state = 'suspended';
    context.dispatchStateChange();
    expect(onStateChange).toHaveBeenLastCalledWith('suspended');
    await controller.resume();
    expect(onStateChange).toHaveBeenLastCalledWith('running');
    controller();
  });
});
