"""Single-file Windows application, with an optional local browser interface."""
if __name__=="__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    from desktop.application import main
    main()
